# -*- coding: utf-8 -*-
"""Конфигурация: дефолты, загрузка с миграцией, атомарное сохранение, валидация."""
import copy
import json
import os
import re
import tempfile

from . import paths

DEFAULTS = {
    "telegram": {
        "api_id": 0,
        "api_hash": "",
        "phone": "",
        "session": "doomsday",
        "game_bot": "@DoomsDayTyrannybot",
        "app_short_name": "play",  # t.me/DoomsDayTyrannybot/play
    },
    "schedules": {
        "reboot_interval_hours": 12,       # запасной интервал (если цикл игры неизвестен)
        "reboot_before_end_minutes": 10,   # авто-ребут за N минут до конца цикла
        "scan_interval_minutes": 60,
        "summary_time": "20:00",
        "retry_failed_minutes": 20,
    },
    "tma": {
        "enabled": True,
        "base_url": "",  # основной домен API игры; подставляется как {{base_url}}
        "timeout_seconds": 25,
        "steps_reboot": [],  # HTTP-шаги ребута производства (см. README)
        "steps_scan": [],    # HTTP-шаги скана состояния/ресурсов
        "resources": {
            "json_path": "",  # путь до массива ресурсов в JSON-ответе скана
            "fields": {"name": "name", "current": "amount", "max": "capacity", "state": "state"},
            "text_patterns": [
                {"name": "Ресурс", "regex": r"([А-Яа-яЁёA-Za-z ]{2,24})[:\s]+(\d[\d\s.,]*)\s*/\s*(\d[\d\s.,]*)", "enabled": True},
            ],
        },
    },
    "chat": {
        "read_last_messages": 20,
        "send_start_on_reboot": True,
        "parse_patterns": [
            {"match": r"[Сс]клад переполнен", "severity": "warn", "notify": True, "title": "Склад переполнен"},
            {"match": r"[Тт]ребуется перезагрузка|ребут", "severity": "warn", "notify": True, "title": "Требуется перезагрузка"},
            {"match": r"[Пп]ерегрев", "severity": "info", "notify": False, "title": "Перегрев производства"},
        ],
    },
    "notify": {
        "enabled": True,
        "warn_threshold_pct": 90,
        "resource_filter": [],  # id ресурсов; пусто = уведомления по всем
        "events": {
            "resource_warn": True,
            "resource_full": True,
            "reboot_report": True,
            "scan_report": False,
            "exchange_report": True,
            "errors": True,
            "daily_summary": True,
            "chat_alerts": True,
        },
        "priority_high": ["resource_full", "errors"],
        "open_game_button": True,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8080,
        "pin": "",
        "open_on_start": True,  # doomsday start открывает панель в браузере
    },
    "security": {
        "night_mode": {
            "enabled": False,
            "from": "01:00",   # с этого часа и до "to" плановые действия не выполняются
            "to": "07:00",
        },
        "jitter": {
            "enabled": True,            # случайные отклонения всех интервалов (анти-паттерн)
            "scan_percent": 15,        # разброс интервала скана ±%
            "reboot_seconds": 180,     # смещение момента ребута ±сек (не ближе 60 с к концу)
            "summary_minutes": 10,     # смещение времени сводки ±мин
            "sell_pause": [0.4, 1.6],  # пауза между продажами, сек [min, max]
        },
    },
    "exchange": {
        "enabled": True,
        "rules": [
            # rid, mode:
            #   "cap"    — обменивать при заполнении склада (threshold_pct)
            #   "always" — продавать всё сразу (как только >= min)
            # keep — сколько единиц оставлять на складе (0 = продавать всё)
            {"rid": "floppy", "mode": "cap", "threshold_pct": 90, "keep": 0, "min": 1, "enabled": True},
            {"rid": "hdd", "mode": "cap", "threshold_pct": 90, "keep": 0, "min": 1, "enabled": True},
            {"rid": "cassete", "mode": "cap", "threshold_pct": 90, "keep": 0, "min": 1, "enabled": True},
            {"rid": "uran_pills", "mode": "always", "threshold_pct": 90, "keep": 0, "min": 1, "enabled": True},
        ],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивное слияние: значения override важнее base, недостающие ключи берутся из base."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# Ключи прошлых версий (zip-обновлятор удалён в v2.0.0) — вычищаем при загрузке,
# чтобы config.json не разбухал и веб-панель не показывала мёртвые настройки.
LEGACY_KEYS = {
    "updater": None,                      # секция целиком
    ("schedules", "update_check_minutes"): None,
    ("notify", "events", "update_applied"): None,
}


def _strip_legacy(cfg: dict) -> dict:
    for key, _ in LEGACY_KEYS.items():
        if isinstance(key, tuple):
            d = cfg
            for part in key[:-1]:
                if not isinstance(d.get(part), dict):
                    d = None
                    break
                d = d[part]
            if isinstance(d, dict):
                d.pop(key[-1], None)
        else:
            cfg.pop(key, None)
    return cfg


def load() -> dict:
    """Загрузить конфиг с подмешиванием дефолтов (миграция на новую версию без потери данных)."""
    user = {}
    try:
        with open(paths.CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            user = {}
    except OSError:
        user = {}
    except ValueError:
        # повреждённый JSON — не теряем: сохраняем бэкап и работаем с дефолтами
        try:
            os.replace(paths.CONFIG_PATH, paths.CONFIG_PATH + ".broken")
        except OSError:
            pass
        user = {}
    return _strip_legacy(_deep_merge(DEFAULTS, user))


def save(cfg: dict) -> dict:
    """Атомарно сохранить конфиг (chmod 600). Возвращает сохранённый словарь."""
    merged = _deep_merge(DEFAULTS, cfg)
    os.makedirs(os.path.dirname(paths.CONFIG_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(paths.CONFIG_PATH), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths.CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return merged


def validate(cfg: dict) -> list:
    """Список человекочитаемых ошибок конфигурации."""
    errs = []
    tg = cfg.get("telegram", {})
    if not tg.get("api_id") or not str(tg.get("api_id", "")).isdigit():
        errs.append("telegram.api_id не задан (получите на my.telegram.org)")
    if not tg.get("api_hash"):
        errs.append("telegram.api_hash не задан")
    if not tg.get("game_bot"):
        errs.append("telegram.game_bot не задан")
    sch = cfg.get("schedules", {})
    if not (0 < float(sch.get("reboot_interval_hours") or 0) <= 168):
        errs.append("schedules.reboot_interval_hours: укажите число часов от 0 до 168")
    if not (0 <= int(sch.get("reboot_before_end_minutes") or 0) <= 360):
        errs.append("schedules.reboot_before_end_minutes: от 0 до 360 минут")
    if not (4 <= int(sch.get("scan_interval_minutes") or 0) <= 1440):
        errs.append("schedules.scan_interval_minutes: от 4 минут до суток")
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(sch.get("summary_time") or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        errs.append("schedules.summary_time: формат ЧЧ:ММ (например 20:00)")
    web = cfg.get("web", {})
    if not (1024 <= int(web.get("port") or 0) <= 65535):
        errs.append("web.port: порт 1024–65535")
    rf = (cfg.get("notify") or {}).get("resource_filter")
    if rf is None:
        rf = []
    if not isinstance(rf, list) or not all(isinstance(x, str) and x for x in rf):
        errs.append("notify.resource_filter: список id ресурсов (строки)")
    for key in ("steps_reboot", "steps_scan"):
        val = (cfg.get("tma") or {}).get(key) or []
        if not isinstance(val, list):
            errs.append(f"tma.{key}: должен быть списком шагов")
            break
    # проверка шагов
    for key in ("steps_reboot", "steps_scan"):
        for i, step in enumerate((cfg.get("tma") or {}).get(key) or []):
            if not isinstance(step, dict) or "url" not in step:
                errs.append(f"tma.{key}[{i}]: нет поля url")
                break
    # правила автообмена
    ex = cfg.get("exchange") or {}
    if not isinstance(ex.get("rules", []), list):
        errs.append("exchange.rules: должен быть списком правил")
    else:
        from . import game_data
        seen_rids = set()
        for i, rule in enumerate(ex.get("rules") or []):
            if not isinstance(rule, dict):
                errs.append(f"exchange.rules[{i}]: должен быть объектом")
                continue
            rid = str(rule.get("rid") or "")
            if rid not in game_data.SELL_INFO:
                errs.append(f"exchange.rules[{i}]: ресурс {rid!r} не продаётся в игре")
                continue
            if rid in seen_rids:
                errs.append(f"exchange.rules[{i}]: правило для {rid!r} уже есть")
            seen_rids.add(rid)
            if str(rule.get("mode")) not in ("cap", "always"):
                errs.append(f"exchange.rules[{i}].mode: 'cap' или 'always'")
            for num_key, lo, hi in (("threshold_pct", 10, 100), ("keep", 0, 10**9), ("min", 0, 10**9)):
                try:
                    v = float(rule.get(num_key) or 0)
                    if not (lo <= v <= hi):
                        raise ValueError
                except (TypeError, ValueError):
                    errs.append(f"exchange.rules[{i}].{num_key}: число от {lo} до {hi}")
    # безопасность: ночной режим и джиттер
    sec = cfg.get("security") or {}
    nm = sec.get("night_mode") or {}
    if nm.get("enabled"):
        for k in ("from", "to"):
            m = re.match(r"^(\d{1,2}):(\d{2})$", str(nm.get(k) or ""))
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                errs.append(f"security.night_mode.{k}: формат ЧЧ:ММ")
        if nm.get("from") == nm.get("to"):
            errs.append("security.night_mode: 'from' и 'to' не должны совпадать")
    jt = sec.get("jitter") or {}
    for num_key, lo, hi in (("scan_percent", 0, 50), ("reboot_seconds", 0, 600),
                           ("summary_minutes", 0, 30)):
        try:
            v = float(jt.get(num_key) if jt.get(num_key) is not None else {"scan_percent": 15,
                                                                           "reboot_seconds": 180,
                                                                           "summary_minutes": 10}[num_key])
            if not (lo <= v <= hi):
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"security.jitter.{num_key}: число от {lo} до {hi}")
    sp = jt.get("sell_pause")
    if sp is not None and (not isinstance(sp, (list, tuple)) or len(sp) != 2
                           or not all(isinstance(x, (int, float)) and 0 <= x <= 30 for x in sp)):
        errs.append("security.jitter.sell_pause: пара чисел [min, max], сек")
    return errs


def masked(cfg: dict) -> dict:
    """Копия конфига для веб-интерфейса: секреты замаскированы."""
    out = copy.deepcopy(cfg)
    tg = out.get("telegram", {})
    if tg.get("api_hash"):
        tg["api_hash"] = "••••••" + str(tg["api_hash"])[-4:]
    return out


def api_hash_stored() -> bool:
    """Есть ли в конфиге реальный api_hash (не маска)."""
    try:
        with open(paths.CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        h = (user.get("telegram") or {}).get("api_hash") or ""
        return bool(h) and "•" not in h
    except (OSError, ValueError):
        return False
