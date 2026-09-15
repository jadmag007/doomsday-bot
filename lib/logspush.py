# -*- coding: utf-8 -*-
"""Выгрузка логов в git: doomsday logs-push + doomsday git-auth.

Философия буфера (по требованию владельца):
  - logs/ и reports/ в установке НИКОГДА не синхронизируются (в .gitignore),
    это локальный буфер устройства;
  - logs-push собирает логи/отчёты/конфиг во временный logsbuf/ внутри git-клона
    исходников (тоже в .gitignore), коммитит их в ОТДЕЛЬНУЮ ветку `logs`
    и пушит в GitHub; основная ветка (main) остаётся чистой — код и логи
    не смешиваются;
  - после успешного пуша: локальный logsbuf/ удаляется, а ОТПРАВЛЕННЫЕ
    логи обрезаются, отчёты удаляются (v2.4.3) — каждая следующая выгрузка
    несёт только записи, накопившиеся после предыдущей, и не путается со
    старыми; разобранные ассистентом бандлы он переносит в archive/ той же
    ветки logs — в корне ветки всегда только неразобранное.

Механика коммита без касания рабочего каталога: временный GIT_INDEX_FILE
(read-tree → git add -f logsbuf/… → write-tree → commit-tree → push).
Локальные правки и текущая ветка не затрагиваются.
"""
import datetime
import getpass
import glob
import json
import os
import platform
import shutil
import subprocess
import tempfile

from . import db
from . import paths

SRC_DIR = os.environ.get("DOOMSDAY_SRC_DIR") or os.path.expanduser("~/doomsday-src")
BUF_DIR = "logsbuf"                      # внутри SRC_DIR, в .gitignore
LOGS_BRANCH = "logs"
TIP_REF = "refs/doomsday/logs-tip"       # локальный указатель на вершину ветки logs
TAIL_LINES = 2000                        # хвост каждого лог-файла
MAX_REPORTS = 50


def _git(args, env_extra=None, timeout=120):
    env = {**os.environ, **(env_extra or {})}
    try:
        return subprocess.run(["git", "-C", SRC_DIR] + args, env=env,
                              capture_output=True, timeout=timeout,
                              text=True, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        class _Fail:
            returncode = -1
            stdout = ""
            stderr = str(e)
        return _Fail()


def _say(msg):
    print(f"▸ {msg}")


def _ok(msg):
    print(f"✓ {msg}")


def _warn(msg):
    print(f"! {msg}")


# ---------------- сборка бандла ----------------

def _tail_file(path: str, out_path: str) -> int:
    """Скопировать хвост файла (не более TAIL_LINES строк). Возвращает размер."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0
    chunk = lines[-TAIL_LINES:]
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(chunk)
    return sum(len(l.encode("utf-8", "ignore")) for l in chunk)


def build_bundle(bundle_dir: str) -> dict:
    """Собрать бандл логов/отчётов/метаданных в bundle_dir. Возвращает манифест."""
    os.makedirs(bundle_dir, exist_ok=True)
    manifest = {"logs": [], "reports": [], "config": [], "bytes": 0}

    # 1) метаданные
    meta = {
        "time": db.now_iso(),
        "version": paths.read_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.node() or "",
        "is_termux": paths.is_termux(),
        "src_head": "",
        "src_dirty": None,
        "origin": "",
    }
    if os.path.isdir(os.path.join(SRC_DIR, ".git")):
        head = _git(["rev-parse", "HEAD"])
        meta["src_head"] = (head.stdout or "").strip()
        st = _git(["status", "--porcelain"])
        meta["src_dirty"] = bool((st.stdout or "").strip())
        url = _git(["remote", "get-url", "origin"])
        meta["origin"] = (url.stdout or "").strip()
    with open(os.path.join(bundle_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    manifest["bytes"] += os.path.getsize(os.path.join(bundle_dir, "meta.json"))

    # 2) логи (хвост каждого файла, включая ротацию bot.log.1 и сервисные)
    logs_dst = os.path.join(bundle_dir, "logs")
    os.makedirs(logs_dst, exist_ok=True)
    for p in sorted(glob.glob(os.path.join(paths.LOG_DIR, "*.log*"))):
        if not os.path.isfile(p):
            continue
        n = _tail_file(p, os.path.join(logs_dst, os.path.basename(p)))
        manifest["logs"].append(os.path.basename(p))
        manifest["bytes"] += n

    # 3) отчёты (последние MAX_REPORTS json-файлов)
    rep_src = sorted(glob.glob(os.path.join(paths.REPORTS_DIR, "*.json")),
                     key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0)
    rep_dst = os.path.join(bundle_dir, "reports")
    os.makedirs(rep_dst, exist_ok=True)
    for p in rep_src[-MAX_REPORTS:]:
        try:
            shutil.copy2(p, os.path.join(rep_dst, os.path.basename(p)))
            manifest["reports"].append(os.path.basename(p))
            manifest["bytes"] += os.path.getsize(p)
        except OSError:
            pass

    # 4) конфиги (в приватном репозитории — осознанно; нужны для диагностики)
    for label, src in (("config-app.json", paths.CONFIG_PATH),
                       ("config-src.json", os.path.join(SRC_DIR, "config.json"))):
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(bundle_dir, label))
                manifest["config"].append(label)
                manifest["bytes"] += os.path.getsize(src)
            except OSError:
                pass
    return manifest


def _prune_old_bundles(keep: int = 3) -> None:
    root = os.path.join(SRC_DIR, BUF_DIR)
    try:
        dirs = sorted(glob.glob(os.path.join(root, "bundle-*")),
                      key=lambda p: os.path.getmtime(p) if os.path.isdir(p) else 0)
        for d in dirs[:-keep] if len(dirs) > keep else []:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _clear_pushed_logs(manifest: dict) -> int:
    """После успешного пуша: обрезать отправленные логи, удалить отправленные
    отчёты. Каждый бандл несёт ХВОСТ текущих логов — без очистки утренние
    записи повторялись бы в каждой выгрузке и путались с новыми (просьба
    владельца 15.09: «чтобы с новыми выгрузками не путались»).

    Безопасность: все писатели логов открывают файлы в append-режиме
    (RotatingFileHandler bot.log, шелл-редирект cron.log/web.log), поэтому
    обрезка на месте (open 'w') корректна — следующие записи пойдут с нуля.
    Файлы, менявшиеся за последние 5 минут, не трогаем: вдруг идёт запись
    (например, активный run-лог из панели). Возвращает число обрезанных.
    """
    import time as _t
    now = _t.time()
    cleared = 0
    for fname in manifest.get("logs") or []:
        p = os.path.join(paths.LOG_DIR, fname)
        try:
            if not os.path.isfile(p) or now - os.path.getmtime(p) < 300:
                continue
            with open(p, "w", encoding="utf-8"):
                pass  # обрезка на месте; append-писатели продолжат с нуля
            cleared += 1
        except OSError:
            pass
    for fname in manifest.get("reports") or []:
        p = os.path.join(paths.REPORTS_DIR, fname)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) >= 300:
                os.unlink(p)
        except OSError:
            pass
    # прун старых run-логов (их пишут и cron-проходы с v2.4.3)
    try:
        files = sorted(glob.glob(os.path.join(paths.LOG_DIR, "run-*.log")),
                       key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0)
        for p in files[:-20] if len(files) > 20 else []:
            os.unlink(p)
    except OSError:
        pass
    return cleared


# ---------------- git-механика: коммит в ветку logs ----------------

def _fetch_tip() -> str:
    """Актуальная вершина ветки logs на origin ('' — ветки нет/сеть недоступна)."""
    r = _git(["fetch", "-q", "origin", f"{LOGS_BRANCH}:{TIP_REF}"], timeout=90)
    if r.returncode == 0:
        tip = _git(["rev-parse", TIP_REF])
        return (tip.stdout or "").strip() if tip.returncode == 0 else ""
    # ветки на origin нет (или сеть) — попробуем локальный указатель прошлой сессии
    tip = _git(["rev-parse", TIP_REF])
    return (tip.stdout or "").strip() if tip.returncode == 0 else ""


def _commit_bundle(bundle_rel: str, parent: str, message: str) -> str:
    """Создать коммит с бандлом поверх parent (или корневой), не трогая рабочий каталог."""
    index_fd, index_path = tempfile.mkstemp(prefix="doomsday-index-", suffix=".idx")
    os.close(index_fd)
    env = {"GIT_INDEX_FILE": index_path}
    try:
        r = _git(["read-tree", parent] if parent else ["read-tree", "--empty"], env_extra=env)
        if r.returncode != 0:
            raise RuntimeError("read-tree: " + (r.stderr or "?")[:200])
        r = _git(["add", "-f", "--", bundle_rel], env_extra=env)
        if r.returncode != 0:
            raise RuntimeError("git add: " + (r.stderr or "?")[:200])
        r = _git(["write-tree"], env_extra=env)
        if r.returncode != 0:
            raise RuntimeError("write-tree: " + (r.stderr or "?")[:200])
        tree = (r.stdout or "").strip()

        author_env = {
            "GIT_AUTHOR_NAME": "doomsday-bot",
            "GIT_AUTHOR_EMAIL": "bot@doomsday.local",
            "GIT_COMMITTER_NAME": "doomsday-bot",
            "GIT_COMMITTER_EMAIL": "bot@doomsday.local",
        }
        cmd = ["commit-tree", tree, "-m", message]
        if parent:
            cmd += ["-p", parent]
        r = _git(cmd, env_extra=author_env)
        if r.returncode != 0:
            raise RuntimeError("commit-tree: " + (r.stderr or "?")[:200])
        return (r.stdout or "").strip()
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def push_logs(message: str = "") -> dict:
    """Собрать бандл и запушить в ветку logs. Возвращает отчёт."""
    result = {"ok": False}
    if not os.path.isdir(os.path.join(SRC_DIR, ".git")):
        result["error"] = (f"{SRC_DIR} — не git-клон. Выгрузка логов работает, когда "
                           "исходники установлены из git (~/doomsday-src)")
        return result
    if not shutil.which("git"):
        result["error"] = "git не найден: pkg install git"
        return result

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle_rel = f"{BUF_DIR}/bundle-{ts}"
    bundle_dir = os.path.join(SRC_DIR, bundle_rel)
    _prune_old_bundles()

    _say("Собираю логи и отчёты…")
    manifest = build_bundle(bundle_dir)
    if not (manifest["logs"] or manifest["reports"]):
        _warn("логов и отчётов не найдено — бандл почти пуст (отправляю метаданные)")

    msg = message or f"logs: bundle-{ts} ({platform.node() or 'device'})"
    parent = _fetch_tip()
    commit = _commit_bundle(bundle_rel, parent, msg)
    if not commit:
        result["error"] = "не удалось создать коммит"
        return result
    result["commit"] = commit

    r = _git(["push", "origin", f"{commit}:refs/heads/{LOGS_BRANCH}"], timeout=120)
    if r.returncode != 0 and "non-fast-forward" in (r.stderr or "") + (r.stdout or ""):
        # кто-то уже пушил логи (другое устройство) — подтягиваем вершину и повторяем
        parent = _fetch_tip()
        commit = _commit_bundle(bundle_rel, parent, msg)
        r = _git(["push", "origin", f"{commit}:refs/heads/{LOGS_BRANCH}"], timeout=120)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "?").strip()[-400:]
        result["error"] = err
        # бандл оставляем на диске (не теряем логи), повторная попытка создаст новый
        result["hint"] = "не удалось push: проверьте сеть и авторизацию (doomsday git-auth)"
        return result

    # успех: локальный буфер чистим — данные теперь в репозитории;
    # отправленные логи обрезаем, чтобы следующая выгрузка содержала
    # только новое (старье уже в ветке logs и не должно повторяться)
    shutil.rmtree(bundle_dir, ignore_errors=True)
    cleared = _clear_pushed_logs(manifest)
    result.update({
        "ok": True, "branch": LOGS_BRANCH, "path": bundle_rel,
        "commit": commit,
        "files": {"logs": manifest["logs"], "reports": len(manifest["reports"]),
                  "config": manifest["config"]},
        "bytes": manifest["bytes"],
        "logs_cleared": cleared,
    })
    db.event("logs-push", "Логи выгружены в git",
             json.dumps({"branch": LOGS_BRANCH, "commit": commit[:12],
                         "bytes": manifest["bytes"]}, ensure_ascii=False))
    return result


def cmd_logs_push(args) -> int:
    res = push_logs(message=getattr(args, "message", "") or "")
    if res.get("ok"):
        _ok(f"Логи отправлены в ветку «{res['branch']}» "
            f"(файлов: {len(res['files']['logs'])} логов + {res['files']['reports']} отчётов, "
            f"{res['bytes'] / 1024:.1f} КБ)")
        _say(f"Коммит: {res['commit'][:12]}  Путь: {res['path']}")
        _say("Скажите ассистенту — он заберёт логи из ветки logs и разберёт их.")
        _say(f"Отправленные логи очищены на устройстве ({res.get('logs_cleared', 0)} шт.) — "
             "следующая выгрузка будет содержать только новое.")
        return 0
    print(f"✘ Выгрузка не удалась: {res.get('error')}")
    if res.get("hint"):
        _warn(res["hint"])
    return 1


# ---------------- git-auth: сохранение PAT для git push/pull ----------------

def _origin_username() -> str:
    """Имя пользователя GitHub из URL origin (для строки credential store)."""
    url = ""
    if os.path.isdir(os.path.join(SRC_DIR, ".git")):
        r = _git(["remote", "get-url", "origin"])
        url = (r.stdout or "").strip()
    # https://github.com/<user>/<repo>.git  или  git@github.com:<user>/<repo>.git
    for sep in ("github.com/", "github.com:"):
        if sep in url:
            tail = url.split(sep, 1)[1]
            user = tail.split("/", 1)[0]
            if user:
                return user
    return "git"


def cmd_git_auth(args) -> int:
    token = getattr(args, "token", "") or ""
    if not token:
        token = getpass.getpass("GitHub PAT (ввод скрыт, не попадёт в историю shell): ").strip()
    if not token:
        print("✘ Токен не введён")
        return 1
    username = _origin_username()
    creds_path = os.path.expanduser("~/.git-credentials")

    # 1) включить хранение учётных данных
    try:
        subprocess.run(["git", "config", "--global", "credential.helper", "store"],
                       check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        print(f"✘ git config не выполнен: {e}")
        return 1

    # 2) перезаписать строку github.com в ~/.git-credentials
    lines = []
    try:
        with open(creds_path, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if "github.com" not in ln]
    except OSError:
        lines = []
    lines.append(f"https://{username}:{token}@github.com")
    with open(creds_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(creds_path, 0o600)
    except OSError:
        pass
    _ok(f"Токен сохранён в {creds_path} (права 600, только это устройство)")
    _ok(f"Теперь git pull/push в {SRC_DIR} пройдет без запроса пароля")

    # 3) проверка связи
    if os.path.isdir(os.path.join(SRC_DIR, ".git")):
        _say("Проверяю доступ к репозиторию…")
        r = _git(["ls-remote", "--heads", "origin"], timeout=60)
        if r.returncode == 0 and (r.stdout or "").strip():
            branches = [ln.split("\t")[1].replace("refs/heads/", "")
                        for ln in (r.stdout or "").strip().splitlines()]
            _ok("Доступ подтверждён. Ветки: " + ", ".join(branches[:5]))
            return 0
        _warn("Проверка не удалась: " + ((r.stderr or r.stdout or "?").strip()[-200:]))
        _warn("Проверьте токен (Contents: Read and Write, только этот репозиторий)")
        return 1
    _warn(f"{SRC_DIR} — не git-клон; токен сохранён и будет использоваться при clone/pull")
    return 0
