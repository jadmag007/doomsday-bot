# -*- coding: utf-8 -*-
"""Временные режимы: ночной режим (бот молчит) и джиттер интервалов.

Зачем: однообразные точные интервалы запросов (ровно T-10:00 до конца цикла,
ровно каждый час) — характерный паттерн автомата. Джиттер задаёт стабильные
псевдослучайные отклонения: детерминированные по seed (одинаковы в cron,
панели и status), но разные для каждого цикла/дня. Ночной режим полностью
приостанавливает плановые действия, чтобы аккаунт «спал» как человек.
"""
import datetime
import re
import zlib


# ---------------- ночной режим ----------------

def _parse_hhmm(s: str):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def night_window(cfg: dict):
    """(включён, from_hh, from_mm, to_hh, to_mm) или (False, ...)."""
    nm = ((cfg.get("security") or {}).get("night_mode")) or {}
    if not nm.get("enabled", False):
        return False, None, None, None, None
    f = _parse_hhmm(nm.get("from", "01:00"))
    t = _parse_hhmm(nm.get("to", "07:00"))
    if not f or not t or f == t:
        return False, None, None, None, None
    return True, f[0], f[1], t[0], t[1]


def night_active(cfg: dict, now: datetime.datetime = None) -> bool:
    """Действует ли ночной режим в момент now (окно может переходить через полночь)."""
    enabled, fh, fm, th, tm = night_window(cfg)
    if not enabled:
        return False
    now = now or datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    frm, to = fh * 60 + fm, th * 60 + tm
    if frm < to:
        return frm <= cur < to
    # окно через полночь: 23:00–06:00
    return cur >= frm or cur < to


# ---------------- джиттер (стабильный, безrandom) ----------------

def _frac16(seed: str) -> float:
    """Стабильное число [0, 1) из строки (crc32 → 16 бит)."""
    return (zlib.crc32(str(seed).encode("utf-8", "replace")) & 0xFFFF) / 65536.0


def jitter_factor(seed: str, percent: float) -> float:
    """Множитель интервала в [1−p/100, 1+p/100]; при p≤0 — ровно 1.

    Одинаков для одного seed в любом месте кода (cron, панель, status),
    но различается между циклами/днями — план и отображение не расходятся.
    """
    try:
        p = float(percent or 0)
    except (TypeError, ValueError):
        p = 0.0
    if p <= 0:
        return 1.0
    return 1.0 + (_frac16(seed) - 0.5) * 2.0 * (p / 100.0)


def jitter_offset(seed: str, seconds: float) -> float:
    """Смещение в [−seconds, +seconds] (стабильное по seed)."""
    try:
        s = float(seconds or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s <= 0:
        return 0.0
    return (_frac16(seed) - 0.5) * 2.0 * s


def jitter_enabled(cfg: dict) -> bool:
    j = ((cfg.get("security") or {}).get("jitter")) or {}
    return bool(j.get("enabled", True))


def scan_interval_sec(cfg: dict, last_ok_iso) -> float:
    """Интервал скана с джиттером (стабильным для текущего цикла)."""
    minutes = int((cfg.get("schedules") or {}).get("scan_interval_minutes", 60) or 60)
    pct = float((((cfg.get("security") or {}).get("jitter")) or {}).get("scan_percent", 15))
    if not jitter_enabled(cfg) or not last_ok_iso:
        return minutes * 60.0
    return minutes * 60.0 * jitter_factor(str(last_ok_iso), pct)


def fallback_reboot_interval_sec(cfg: dict, last_ok_iso) -> float:
    """Запасной интервал ребута (когда цикл игры неизвестен) с джиттером."""
    hours = float((cfg.get("schedules") or {}).get("reboot_interval_hours", 12) or 12)
    pct = float((((cfg.get("security") or {}).get("jitter")) or {}).get("scan_percent", 15))
    if not jitter_enabled(cfg) or not last_ok_iso:
        return hours * 3600.0
    return hours * 3600.0 * jitter_factor(str(last_ok_iso), pct)


def reboot_before_sec(cfg: dict, ends_ms) -> float:
    """За сколько секунд до конца цикла открывать окно ребута — с джиттером.

    Отклонение ±reboot_seconds (по умолчанию 180), но окно никогда не
    смещается ближе 60 секунд к концу цикла — фарм не должен прерываться.
    """
    before_min = int((cfg.get("schedules") or {}).get("reboot_before_end_minutes", 10) or 0)
    base = max(0, before_min) * 60.0
    sec = float((((cfg.get("security") or {}).get("jitter")) or {}).get("reboot_seconds", 180))
    if jitter_enabled(cfg) and ends_ms:
        base += jitter_offset(str(int(ends_ms)), sec)
    return max(60.0, base)


def summary_minute_offset(cfg: dict, date_iso: str) -> int:
    """Смещение времени daily-сводки в минутах (стабильное в течение дня)."""
    minutes = float((((cfg.get("security") or {}).get("jitter")) or {}).get("summary_minutes", 10))
    if not jitter_enabled(cfg) or minutes <= 0:
        return 0
    return int(round(jitter_offset(str(date_iso), minutes)))


def sell_pause_sec(cfg: dict, seed: str) -> float:
    """Пауза между продажами в автообмене: [0.4, 1.6] с ±-вариации по seed.

    Настоящий random здесь уместен: значение ни с чем не сверяется и живёт
    секунды — важно лишь отсутствие одинаковых пауз подряд.
    """
    import random
    j = ((cfg.get("security") or {}).get("jitter")) or {}
    rng = j.get("sell_pause") or [0.4, 1.6]
    try:
        lo, hi = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError):
        lo, hi = 0.4, 1.6
    if hi < lo:
        lo, hi = hi, lo
    return lo + random.Random(seed).random() * (hi - lo)
