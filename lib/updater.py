# -*- coding: utf-8 -*-
"""Автообновление: ищем doomsday-bot-v*.zip в Загрузках телефона и применяем новее текущей.

Безопасность:
 - архив распаковывается во временный staging и валидируется (VERSION, lib/);
 - текущий код копируется в .backup/<время>/ перед заменой, при ошибке — откат;
 - config.json / state.db / logs / session / venv / .updates не трогаются;
 - конфиг мигрируется (недостающие ключи дополняются дефолтами);
 - применённые архивы запоминаются по sha256 (повторно не ставятся, даже если
   файл в Загрузках не удалось удалить — Android 11+ иногда не даёт).
"""
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import zipfile

from . import config as cfgmod
from . import db
from . import notify as notify_mod
from . import paths

log = logging.getLogger("doomsday.updater")


def parse_version(name: str):
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", os.path.basename(name))
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def version_cmp(a, b) -> int:
    return (a > b) - (a < b)


def find_archives(directory: str, pattern: str = "doomsday-bot-v*.zip") -> list:
    """Все подходящие архивы, отсортированные по версии (свежие в конце)."""
    out = []
    try:
        for p in glob.glob(os.path.join(directory, pattern)):
            v = parse_version(p)
            if v:
                out.append((v, p))
    except OSError as e:
        log.warning("Не могу прочитать каталог %s: %s", directory, e)
    out.sort(key=lambda x: x[0])
    return out


def applied_archives() -> list:
    return db.kv_get("applied_archives", []) or []


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 512), b""):
            h.update(chunk)
    return h.hexdigest()


def _executables() -> list:
    return [
        os.path.join("bin", "doomsday"), "install.sh", "update.sh", "uninstall.sh",
        "start.sh", os.path.join("service", "run"), os.path.join("boot", "doomsday-boot.sh"),
    ]


def check_and_apply(cfg: dict, force: bool = False) -> dict:
    """Точка входа: найти самый свежий архив и применить, если он новее."""
    up = cfg.get("updater", {}) or {}
    if not up.get("enabled", True):
        return {"checked": False, "reason": "апдейтер выключен в настройках"}
    directory = paths.resolve_download_dir(up.get("download_dir", ""))
    archives = find_archives(directory, up.get("pattern", "doomsday-bot-v*.zip"))
    if not archives:
        return {"checked": True, "found": 0, "directory": directory}
    ver, path = archives[-1]
    current = parse_version("v" + paths.read_version()) or (0, 0, 0)
    digest = _sha256(path)
    known = {a.get("sha256") for a in applied_archives()}
    if digest in known and not force:
        return {"checked": True, "found": len(archives), "skipped": "архив уже применён",
                "version": ".".join(map(str, ver))}
    if version_cmp(ver, current) <= 0 and not force:
        return {"checked": True, "found": len(archives), "current": ".".join(map(str, current)),
                "newest": ".".join(map(str, ver)), "skipped": "нет более новой версии"}
    try:
        result = apply_archive(path, cfg)
        return {"checked": True, "found": len(archives), **result}
    except Exception as e:
        log.exception("Обновление провалилось: %s", e)
        notify_mod.notify_error(cfg, "Обновление провалилось", str(e)[:300])
        return {"checked": True, "found": len(archives), "error": str(e)}


def apply_archive(zip_path: str, cfg: dict) -> dict:
    """Применить конкретный архив: staging → валидация → бэкап → замена → миграция → рестарт."""
    ver_from_name = parse_version(zip_path)
    # 1) распаковка в staging
    if os.path.isdir(paths.STAGING_DIR):
        shutil.rmtree(paths.STAGING_DIR, ignore_errors=True)
    os.makedirs(paths.STAGING_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for n in names:
            if n.startswith("/") or ".." in n:  # защита от zip-slip
                raise ValueError(f"подозрительный путь в архиве: {n}")
        zf.extractall(paths.STAGING_DIR)
    # 2) валидация
    staging = paths.STAGING_DIR
    inner = staging
    if not os.path.exists(os.path.join(inner, "VERSION")) and os.path.isdir(
            os.path.join(staging, "doomsday-bot")):
        inner = os.path.join(staging, "doomsday-bot")  # архив с корневой папкой
    if not os.path.exists(os.path.join(inner, "VERSION")) or not os.path.isdir(os.path.join(inner, "lib")):
        raise ValueError("архив не похож на обновление doomsday-bot (нет VERSION/lib)")
    new_version = open(os.path.join(inner, "VERSION"), encoding="utf-8").read().strip()
    if ver_from_name and parse_version("v" + new_version) != ver_from_name:
        raise ValueError(f"версия в имени {ver_from_name} не совпадает с VERSION {new_version}")

    # 3) бэкап текущего кода
    stamp = db.now_iso().replace(":", "").replace("-", "")
    backup = os.path.join(paths.BACKUP_DIR, stamp)
    os.makedirs(backup, exist_ok=True)
    for entry in paths.CODE_ENTRIES:
        src = os.path.join(paths.APP_DIR, entry)
        if os.path.exists(src):
            dst = os.path.join(backup, entry)
            try:
                shutil.move(src, dst)
            except OSError:
                shutil.copytree(src, dst, dirs_exist_ok=True) if os.path.isdir(src) else shutil.copy2(src, dst)

    # 4) замена кода
    try:
        for entry in paths.CODE_ENTRIES:
            src = os.path.join(inner, entry)
            if not os.path.exists(src):
                continue
            dst = os.path.join(paths.APP_DIR, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        for rel in _executables():
            p = os.path.join(paths.APP_DIR, rel)
            if os.path.exists(p):
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
    except Exception:
        # откат из бэкапа
        for entry in paths.CODE_ENTRIES:
            src = os.path.join(backup, entry)
            dst = os.path.join(paths.APP_DIR, entry)
            if os.path.exists(src):
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
        raise

    # 5) миграция конфига под новую версию
    cfg = cfgmod.load()
    cfgmod.save(cfg)

    # 6) зависимости, если requirements.txt изменился
    req_changed = _requirements_changed(backup)
    pip_log = ""
    if req_changed:
        pip_log = _pip_install()

    # 7) рестарт веб-панели
    restarted = _restart_web(cfg)

    # 8) учёт архива: перенос в .updates (или пометка, если удалить нельзя)
    digest = _sha256(zip_path)
    applied = applied_archives()
    applied.append({"name": os.path.basename(zip_path), "sha256": digest,
                    "version": new_version, "ts": db.now_iso()})
    keep = int((cfg.get("updater", {}) or {}).get("keep_applied", 5) or 5)
    db.kv_set("applied_archives", applied[-keep * 2:])
    _absorb_archive(zip_path, keep)

    # 9) чистка staging и старых бэкапов
    shutil.rmtree(paths.STAGING_DIR, ignore_errors=True)
    _prune_backups(5)

    db.event("update", f"Обновление до v{new_version} применено",
             f"Архив: {os.path.basename(zip_path)}; зависимости: "
             f"{'обновлены' if req_changed else 'без изменений'}; "
             f"веб-панель: {'перезапущена' if restarted else 'не перезапускалась'}; {pip_log}")
    if cfg.get("notify", {}).get("events", {}).get("update_applied", True):
        notify_mod.notify(cfg, "update_applied", f"Обновлён до v{new_version}",
                          "Автообновление из Загрузок применено успешно.")
    return {"applied": True, "version": new_version, "pip": pip_log,
            "web_restarted": restarted, "requirements_changed": req_changed}


def _requirements_changed(backup: str) -> bool:
    old = os.path.join(backup, "requirements.txt")
    new = paths.REQUIREMENTS_PATH
    try:
        with open(old, encoding="utf-8") as f:
            a = f.read().strip()
        with open(new, encoding="utf-8") as f:
            b = f.read().strip()
        return a != b
    except OSError:
        return True  # не смогли сравнить — лучше переустановить


def _pip_install() -> str:
    py = paths.venv_python()
    try:
        res = subprocess.run(
            [py, "-m", "pip", "install", "-r", paths.REQUIREMENTS_PATH, "--quiet"],
            capture_output=True, timeout=600)
        if res.returncode == 0:
            return "pip: OK"
        return "pip: ошибка " + res.stderr.decode(errors="ignore")[-300:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"pip: {e}"


def _restart_web(cfg: dict) -> bool:
    if not (cfg.get("updater", {}) or {}).get("restart_web_on_update", True):
        return False
    try:
        subprocess.run(["sv", "restart", "doomsday-web"], capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.run(["pkill", "-f", "webui"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _absorb_archive(zip_path: str, keep: int) -> None:
    """Забрать архив из Загрузок в .updates (если Android позволяет удалять)."""
    os.makedirs(paths.UPDATES_DIR, exist_ok=True)
    dst = os.path.join(paths.UPDATES_DIR, os.path.basename(zip_path))
    try:
        shutil.move(zip_path, dst)
    except OSError:
        try:
            shutil.copy2(zip_path, dst)
            os.unlink(zip_path)
        except OSError:
            # оставить в Загрузках — повторное применение исключено по sha256
            pass
    # подчистить старые
    try:
        old = sorted(glob.glob(os.path.join(paths.UPDATES_DIR, "doomsday-bot-v*.zip")),
                     key=lambda p: parse_version(p) or (0, 0, 0))
        for p in old[:-keep] if len(old) > keep else []:
            os.unlink(p)
    except OSError:
        pass


def _prune_backups(keep: int) -> None:
    try:
        dirs = sorted(glob.glob(os.path.join(paths.BACKUP_DIR, "*")))
        for d in dirs[:-keep] if len(dirs) > keep else []:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _cli() -> int:
    """python -m lib.updater [--apply архив.zip] [--force]"""
    import argparse

    from . import worker  # отложенный импорт после настройки логов

    worker.setup_logging()
    p = argparse.ArgumentParser(prog="updater")
    p.add_argument("--apply", help="применить конкретный архив")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    cfg = cfgmod.load()
    if args.apply:
        res = {"applied": False}
        try:
            out = apply_archive(args.apply, cfg)
            res.update(out)
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            return 0
        except Exception as e:
            notify_mod.notify_error(cfg, "Обновление провалилось", str(e)[:300])
            print(json.dumps({"applied": False, "error": str(e)}, ensure_ascii=False))
            return 1
    res = check_and_apply(cfg, force=args.force)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0 if not res.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
