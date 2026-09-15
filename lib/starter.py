# -*- coding: utf-8 -*-
"""Стартёр и менеджер служб: doomsday start / doomsday stop.

Логика стартёра (раньше жил в start.sh, теперь единая точка — здесь):
  1. git pull --ff-only в ~/doomsday-src (если исходники — git-клон);
  2. при смене VERSION — тихая переустановка install.sh (config/state не трогаются);
  3. wake-lock + сервисы: sv (termux-services) или фоновые процессы с pid-файлами;
  4. контрольный проход cron (догоняем пропущенное во время простоя).

start.sh остаётся тонкой обёрткой для Termux:Boot: exec bin/doomsday start.
"""
import datetime
import os
import shutil
import subprocess

from . import config as cfgmod
from . import db
from . import paths

SRC_DIR = os.environ.get("DOOMSDAY_SRC_DIR") or os.path.expanduser("~/doomsday-src")
RUN_DIR = os.path.join(paths.APP_DIR, "run")
CRON_MARK = "# doomsday-bot"

_sv = shutil.which("sv")


def _say(msg):
    print(f"▸ {msg}")


def _ok(msg):
    print(f"✓ {msg}")


def _warn(msg):
    print(f"! {msg}")


def _git(args, timeout=90):
    """git -C SRC_DIR ... с захватом вывода."""
    try:
        return subprocess.run(
            ["git", "-C", SRC_DIR] + args,
            capture_output=True, timeout=timeout, text=True, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        class _Fail:
            returncode = -1
            stdout = ""
            stderr = str(e)
        return _Fail()


# ---------------- обновление из git ----------------

def update_from_git() -> bool:
    """git pull + переустановка при смене версии. True — код обновлён."""
    if not (os.path.isdir(os.path.join(SRC_DIR, ".git")) and shutil.which("git")):
        return False
    _say("Проверяю обновления на GitHub…")
    r = _git(["pull", "--ff-only", "-q"])
    if r.returncode != 0:
        _warn("git pull не удался (нет сети/нужен вход?) — продолжаю на текущей версии")
        if "Authentication" in (r.stderr or "") or "could not read Username" in (r.stderr or ""):
            _warn("Авторизация: выполните один раз  doomsday git-auth")
        return False
    new_ver = _read_file(os.path.join(SRC_DIR, "VERSION")).strip() or "?"
    cur_ver = paths.read_version()
    if new_ver == cur_ver or not os.path.isfile(os.path.join(SRC_DIR, "install.sh")):
        _say(f"обновлений нет ({cur_ver})")
        return False
    _say(f"Новая версия: {new_ver} (установлена: {cur_ver}) — переустанавливаю…")
    try:
        res = subprocess.run(["sh", os.path.join(SRC_DIR, "install.sh")],
                             capture_output=True, timeout=900)
        if res.returncode == 0:
            _ok(f"обновлён до {new_ver}")
            db.event("start", f"Обновление до v{new_ver}",
                     "Стартёр обновил код из git и переустановил")
            return True
        _warn("переустановка не удалась — продолжаю на текущей версии")
    except (OSError, subprocess.TimeoutExpired) as e:
        _warn(f"переустановка не удалась ({e}) — продолжаю на текущей версии")
    return False


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---------------- фоновые процессы (без termux-services) ----------------

def _pidfile(name: str) -> str:
    return os.path.join(RUN_DIR, f"{name}.pid")


def _pid_alive(pid, needle: str = "") -> bool:
    """Процесс жив и (опционально) в его cmdline есть needle."""
    if not pid or not str(pid).strip().isdigit():
        return False
    pid = int(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if needle:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", errors="replace").replace("\0", " ")
            return needle in cmd
        except OSError:
            return True
    return True


def _read_pid(name: str):
    try:
        with open(_pidfile(name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_pid(name: str, pid: int) -> None:
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        with open(_pidfile(name), "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        pass


def _spawn(name: str, args: list, log_name: str):
    """Запустить процесс в фоне (своя сессия, вывод — в logs/<log_name>)."""
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    log_path = os.path.join(paths.LOG_DIR, log_name)
    log_f = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            args, stdout=log_f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "DOOMSDAY_BOT_DIR": paths.APP_DIR},
            cwd=paths.APP_DIR)
        _write_pid(name, proc.pid)
        return proc.pid
    finally:
        log_f.close()


def _kill_pid(name: str, needle: str) -> bool:
    pid = _read_pid(name)
    if _pid_alive(pid, needle):
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass
        return True
    # подчистить мёртвый pid-файл
    try:
        os.unlink(_pidfile(name))
    except OSError:
        pass
    return False


def _web_running() -> bool:
    return _pid_alive(_read_pid("web"), "doomsday")


# ---------------- cron-строка ----------------

def cron_ensure() -> None:
    """Убедиться, что строка doomsday-bot есть в crontab (идемпотентно)."""
    if not shutil.which("crontab"):
        return
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, errors="replace")
        lines = (cur.stdout or "").splitlines()
        if any(CRON_MARK in ln for ln in lines):
            return
        runline = (f"{CRON_MARK} */10 * * * * {paths.BIN_DOOMSDAY} cron "
                   f">> {paths.LOG_DIR}/cron.log 2>&1")
        new = "\n".join([ln for ln in lines if ln.strip()] + [runline]) + "\n"
        subprocess.run(["crontab", "-"], input=new, text=True, capture_output=True, timeout=30)
        _ok("cron: строка doomsday-bot восстановлена (каждые 10 минут)")
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass


def cron_disable() -> None:
    """Убрать строку doomsday-bot из crontab (для doomsday stop)."""
    if not shutil.which("crontab"):
        return
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, errors="replace")
        lines = [ln for ln in (cur.stdout or "").splitlines() if CRON_MARK not in ln]
        new = "\n".join(lines) + ("\n" if lines else "")
        subprocess.run(["crontab", "-"], input=new, text=True, capture_output=True, timeout=30)
        _ok("cron: строка doomsday-bot убрана (плановые проходы остановлены)")
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass


# ---------------- службы ----------------

def services_up(code_updated: bool) -> None:
    # wake-lock: не даём Android усыпить Termux
    wl = shutil.which("termux-wake-lock")
    if wl:
        try:
            subprocess.run([wl], capture_output=True, timeout=15)
            _ok("wake-lock активен")
        except (OSError, subprocess.TimeoutExpired):
            pass

    if _sv:
        try:
            up = subprocess.run(["sv", "up", "doomsday-web"], capture_output=True, timeout=30)
            if up.returncode != 0 or code_updated:
                subprocess.run(["sv", "restart", "doomsday-web"], capture_output=True, timeout=30)
            subprocess.run(["sv", "up", "cronie"], capture_output=True, timeout=30)
            _ok("сервисы подняты (doomsday-web, cronie)")
        except (OSError, subprocess.TimeoutExpired):
            _warn("sv вызов не удался — проверьте termux-services")
    else:
        if code_updated:
            _kill_pid("web", "doomsday")
        if _web_running():
            _ok("веб-панель уже работает (фон)")
        elif os.path.isfile(paths.BIN_DOOMSDAY):
            _spawn("web", [paths.BIN_DOOMSDAY, "web"], "web.log")
            _ok("веб-панель запущена в фоне (termux-services не найден)")
        else:
            _warn(f"нет {paths.BIN_DOOMSDAY} — веб-панель не запущена")


def services_down() -> None:
    if _sv:
        try:
            subprocess.run(["sv", "down", "doomsday-web"], capture_output=True, timeout=30)
            _ok("сервис doomsday-web остановлен (sv down)")
        except (OSError, subprocess.TimeoutExpired):
            pass
    if _kill_pid("web", "doomsday"):
        _ok("фоновая веб-панель остановлена")
    _kill_pid("cron", "doomsday")


# ---------------- команды CLI ----------------

def _panel_url(cfg: dict) -> str:
    web = cfg.get("web", {})
    return f"http://{web.get('host', '127.0.0.1')}:{web.get('port', 8080)}"


def cmd_start(args) -> int:
    cfg = cfgmod.load()
    print(f"Doomsday Tyranny Bot v{paths.read_version()} — стартёр")
    print(f"Установка: {paths.APP_DIR}")
    print(f"Исходники: {SRC_DIR}\n")

    code_updated = update_from_git()
    services_up(code_updated)
    cron_ensure()

    # контрольный проход: догоняем пропущенное (действия решат сами, пора ли)
    if os.path.isfile(paths.BIN_DOOMSDAY):
        try:
            _spawn("cron", [paths.BIN_DOOMSDAY, "cron"], "cron.log")
            _say("контрольный проход cron запущен в фоне")
        except OSError as e:
            _warn(f"контрольный проход не запущен: {e}")

    print()
    _ok(f"doomsday-bot работает. Веб-панель: {_panel_url(cfg)}")
    _say("логи: ~/doomsday-bot/logs (не синхронизируются)")
    _say("передать логи разработчику: doomsday logs-push")
    db.event("start", "Стартёр выполнен",
             f"код {'обновлён' if code_updated else 'без изменений'}; "
             f"панель {_panel_url(cfg)}")
    return 0


def cmd_stop(args) -> int:
    print("Останавливаю doomsday-bot…")
    services_down()
    cron_disable()
    wl = shutil.which("termux-wake-unlock")
    if wl and os.environ.get("DOOMSDAY_KEEP_WAKELOCK") != "1":
        try:
            subprocess.run([wl], capture_output=True, timeout=15)
            _say("wake-lock снят")
        except (OSError, subprocess.TimeoutExpired):
            pass
    _ok("Остановлено. Запуск снова: doomsday start (или перезагрузка телефона)")
    db.event("stop", "Остановка по команде", "doomsday stop: службы и cron остановлены")
    return 0


def farm_deadline_info() -> dict:
    """Данные о цикле производства для панели/сводки (из kv, пишется scan/reboot)."""
    import time as _time
    ends_raw = db.kv_get("passive_farm_ends_at")
    if not ends_raw:
        return {"known": False}
    try:
        ends_ms = float(ends_raw)
    except ValueError:
        return {"known": False}
    left_sec = int(ends_ms / 1000 - _time.time())
    return {
        "known": True,
        "ends_at": datetime.datetime.fromtimestamp(ends_ms / 1000).isoformat(timespec="seconds"),
        "left_sec": left_sec,
        "active": left_sec > 0,
    }
