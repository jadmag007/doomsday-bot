# -*- coding: utf-8 -*-
"""Пути приложения. Всё живёт в каталоге установки (по умолчанию ~/doomsday-bot)."""
import os
import sys

APP_DIR = os.environ.get("DOOMSDAY_BOT_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DB_PATH = os.path.join(APP_DIR, "state.db")
VERSION_PATH = os.path.join(APP_DIR, "VERSION")
REQUIREMENTS_PATH = os.path.join(APP_DIR, "requirements.txt")
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "bot.log")
CRON_LOG_PATH = os.path.join(LOG_DIR, "cron.log")
SESSION_DIR = os.path.join(APP_DIR, "session")
REPORTS_DIR = os.path.join(APP_DIR, "reports")
WEB_DIR = os.path.join(APP_DIR, "web")
BIN_DIR = os.path.join(APP_DIR, "bin")
BIN_DOOMSDAY = os.path.join(BIN_DIR, "doomsday")
LOCK_PATH = os.path.join(APP_DIR, "worker.lock")

def ensure_dirs():
    """Создать служебные каталоги."""
    for d in (LOG_DIR, SESSION_DIR, REPORTS_DIR, os.path.dirname(LOCK_PATH)):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    try:
        os.chmod(SESSION_DIR, 0o700)
    except OSError:
        pass


def read_version() -> str:
    try:
        with open(VERSION_PATH, "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def venv_python() -> str:
    """Путь к python из виртуального окружения (с фолбэком на системный)."""
    cand = os.path.join(APP_DIR, "venv", "bin", "python")
    if os.path.exists(cand):
        return cand
    return sys.executable or "python3"


def is_termux() -> bool:
    return (
        os.environ.get("TERMUX_VERSION") is not None
        or os.environ.get("PREFIX", "").endswith("com.termux/files/usr")
        or os.path.exists("/data/data/com.termux")
    )
