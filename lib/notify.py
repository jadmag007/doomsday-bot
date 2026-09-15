# -*- coding: utf-8 -*-
"""Уведомления: termux-notification (Android) с журналированием в БД."""
import logging
import shutil
import subprocess

from . import db

log = logging.getLogger("doomsday.notify")


def termux_notification_available() -> bool:
    return shutil.which("termux-notification") is not None


def game_url(cfg: dict) -> str:
    bot = (cfg.get("telegram", {}).get("game_bot") or "doomsdaytyrannybot").lstrip("@")
    return f"https://t.me/{bot}/play"


def notify(cfg: dict, kind: str, title: str, body: str = "", high: bool = False,
           open_game: bool = False) -> bool:
    """Отправить push-уведомление и записать событие в журнал.

    Возвращает True, если push реально доставлен в termux-notification.
    """
    sev = "critical" if high else "info"
    db.event("notify:" + kind, title, body, severity=sev)
    if not (cfg.get("notify", {}).get("enabled", True)):
        return False
    enabled_map = cfg.get("notify", {}).get("events", {})
    # kind может быть составным (например notify:resource_full) — проверяем базовое имя
    if kind in enabled_map and not enabled_map.get(kind, True):
        return False
    high_kinds = cfg.get("notify", {}).get("priority_high", []) or []
    force_high = kind in high_kinds or high
    bin_path = shutil.which("termux-notification")
    if not bin_path:
        log.warning("termux-notification не найден (установите пакет termux-api и приложение Termux:API)")
        return False
    args = [bin_path, "--title", f"Doomsday Tyranny: {title}", "--content", body or title]
    try:
        if force_high:
            args += ["--priority", "high", "--vibrate", "500,200,500", "--sound"]
        else:
            args += ["--priority", "default"]
        if open_game and (cfg.get("notify", {}).get("open_game_button", True)):
            args += [
                "--button1", "Открыть игру",
                "--button1-action", f"termux-open-url '{game_url(cfg)}'",
            ]
        res = subprocess.run(args, capture_output=True, timeout=15)
        if res.returncode != 0:
            err = res.stderr.decode(errors="ignore")[:300]
            log.error("termux-notification вернул ошибку: %s", err)
            db.event("notify_fail", "Не удалось отправить push", err, severity="warn")
            return False
        return True
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        log.error("termux-notification: таймаут (приложение Termux:API не отвечает?)")
        return False


def notify_error(cfg: dict, title: str, body: str = "") -> None:
    """Критическое уведомление об ошибке (если включены уведомления об ошибках)."""
    notify(cfg, "errors", title, body, high=True)
