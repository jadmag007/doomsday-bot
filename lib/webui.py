# -*- coding: utf-8 -*-
"""Веб-панель управления: чистый stdlib (http.server), без внешних зависимостей.

Отдаёт web/ (SPA) и JSON API:
  GET  /api/overview          — таймеры, ресурсы, версия, статусы
  GET  /api/config            — конфиг (api_hash замаскирован)
  PUT  /api/config            — сохранить конфиг
  POST /api/action            — запустить действие (reboot/scan/summary/discover/check/test-notify)
  GET  /api/runs, /api/runs/N — журнал запусков и лог конкретного запуска
  GET  /api/events            — журнал событий
  GET  /api/log               — хвост bot.log
  GET  /api/discovery         — последний отчёт discovery
  POST /api/restart-web       — перезапуск сервиса панели
  POST /api/login             — вход по PIN (если задан)
"""
import datetime
import hashlib
import json
import logging
import math
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config as cfgmod
from . import db
from . import paths

log = logging.getLogger("doomsday.webui")

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".woff2": "font/woff2", ".woff": "font/woff", ".webp": "image/webp",
        ".map": "application/json"}

MAX_BODY = 512 * 1024

# Версия кода РАБОТАЮЩЕГО процесса (фиксируется при импорте). Поле backend
# в /api/overview позволяет отличить старый «висячий» процесс от свежего:
# read_version() в рантайме читает файл с диска и всегда «новая».
BACKEND_VERSION = paths.read_version()


# ---------------- вспомогательное ----------------

def _web_token(cfg: dict) -> str:
    salt = db.kv_get("web_salt")
    if not salt:
        salt = secrets.token_hex(16)
        db.kv_set("web_salt", salt)
    pin = str(cfg.get("web", {}).get("pin") or "")
    return hashlib.sha256((pin + ":" + salt).encode()).hexdigest() if pin else ""


def _authed(handler, cfg: dict) -> bool:
    pin = str(cfg.get("web", {}).get("pin") or "")
    if not pin:
        return True
    cookies = handler.headers.get("Cookie", "") or ""
    for part in cookies.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ddt_auth" and secrets.compare_digest(v, _web_token(cfg)):
            return True
    return False


def _iso_min(ts: str):
    return (ts or "")[:16].replace("T", " ")


def spawn_action(action: str) -> int:
    """Запустить `bin/doomsday <action>` отдельным процессом, вернуть id запуска."""
    rid = db.run_start(action)
    log_path = os.path.join(paths.LOG_DIR, f"run-{rid}.log")
    try:
        f = open(log_path, "w", encoding="utf-8")
    except OSError:
        f = None
    try:
        p = subprocess.Popen(
            [paths.BIN_DOOMSDAY, action],
            stdout=f, stderr=subprocess.STDOUT, cwd=paths.APP_DIR,
            env={**os.environ, "PATH": os.environ.get("PREFIX", "/data/data/com.termux/files/usr") + "/bin:"
                 + os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except OSError as e:
        if f:
            f.write(f"Не удалось запустить: {e}\n")
            f.close()
        db.run_finish(rid, "fail", str(e))
        return rid

    def reaper():
        try:
            p.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        tail = ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as fh:
                tail = fh.read()[-16000:]
        except OSError:
            pass
        if f:
            f.close()
        db.run_finish(rid, "ok" if p.returncode == 0 else "fail", tail)

    threading.Thread(target=reaper, daemon=True).start()
    return rid


# ---------------- overview ----------------

def build_overview(cfg: dict) -> dict:
    import datetime
    from . import starter as starter_mod
    from . import tma as tma_mod
    from . import game_data
    from . import timing
    now = datetime.datetime.now()

    def since(key):
        v = db.kv_get(key)
        try:
            return datetime.datetime.fromisoformat(v) if v else None
        except ValueError:
            return None

    minutes = int(cfg.get("schedules", {}).get("scan_interval_minutes", 60) or 60)
    last_scan = since("last_scan_ts")
    # следующий скан — с тем же стабильным джиттером, что и в cron (план = отображение)
    if last_scan:
        scan_interval = timing.scan_interval_sec(cfg, db.kv_get("last_scan_ts"))
        next_scan = last_scan + datetime.timedelta(seconds=scan_interval)
    else:
        next_scan = None
    # контрольный скан после ребута может быть раньше регулярного
    force_at = since("force_scan_at")
    if force_at and (next_scan is None or force_at < next_scan):
        next_scan = force_at
    hours = float(cfg.get("schedules", {}).get("reboot_interval_hours", 12) or 12)
    resources = []
    for r in db.resources_latest_ru():
        mx = r.get("maximum")
        pct = round((r.get("current") or 0) / mx * 100, 1) if mx else None
        resources.append({"id": r.get("id"), "name": r["name"], "current": r.get("current"),
                          "max": mx, "pct": pct, "state": r.get("state") or "",
                          "ts": _iso_min(r.get("ts"))})
    discovery = db.kv_get("discovery") or {}
    running = db.running_run()
    checks = db.events_list(limit=5, kind="check")
    farm = starter_mod.farm_deadline_info(cfg)
    coin, mcoin = db.kv_get("balance_coin"), db.kv_get("balance_mcoin")
    ex_cfg = cfg.get("exchange") or {}
    last_ex = db.events_list(limit=5, kind="exchange")
    exchange = {
        "enabled": bool(ex_cfg.get("enabled", True)),
        "rules": ex_cfg.get("rules") or [],
        "sellable": game_data.sellable_catalog(),
        "balances": {
            "coin": coin,
            "coin_ru": game_data.fmt_bytes(coin) if coin is not None else None,
            "mcoin": mcoin,
            "mcoin_ru": (f"{mcoin:g} DDT" if mcoin is not None else None),
        },
        "last": [{"ts": e.get("ts"), "title": e.get("title"),
                   "body": e.get("body")} for e in last_ex],
    }
    # безопасность + здоровье cron + ETA DDT-ресурсов (для статусбара)
    security = {
        "night_enabled": bool(((cfg.get("security") or {}).get("night_mode") or {}).get("enabled")),
        "night_active": timing.night_active(cfg, now),
        "night_from": ((cfg.get("security") or {}).get("night_mode") or {}).get("from"),
        "night_to": ((cfg.get("security") or {}).get("night_mode") or {}).get("to"),
        "jitter_enabled": timing.jitter_enabled(cfg),
    }
    last_cron = since("last_cron_ts")
    cron_alive = last_cron is not None and (now - last_cron).total_seconds() <= 25 * 60
    ddt_eta = db.kv_get("ddt_eta") or {}
    if isinstance(ddt_eta, str):   # страховка от двойного кодирования
        try:
            ddt_eta = json.loads(ddt_eta)
        except ValueError:
            ddt_eta = {}
    timers = {
        "last_reboot": _iso_min(db.kv_get("last_reboot_ts")),
        "last_scan": _iso_min(db.kv_get("last_scan_ts")),
        "next_scan": next_scan.isoformat(timespec="seconds") if next_scan else None,
        "scan_in_sec": max(0, int((next_scan - now).total_seconds())) if next_scan else None,
        "last_summary": db.kv_get("last_summary_date"),
        "scan_interval_minutes": minutes,
        "last_cron_ts": _iso_min(db.kv_get("last_cron_ts")),
        "cron_alive": cron_alive,
        "farm_cycle": {
            "known": farm.get("known", False),
            "ends_at": farm.get("ends_at"),
            "left_sec": farm.get("left_sec"),
            "active": farm.get("active"),
            "reboot_at": farm.get("reboot_at"),
            "reboot_in_sec": farm.get("reboot_in_sec"),
            "before_min": farm.get("before_min"),
            "reboot_jitter_sec": farm.get("reboot_jitter_sec"),
            "total_sec": int(hours * 3600),
        },
    }
    return {
        "version": paths.read_version(),
        "backend": BACKEND_VERSION,
        "now": db.now_iso(),
        "timers": timers,
        "resources": resources,
        "resource_filter": (cfg.get("notify", {}).get("resource_filter") or []),
        "exchange": exchange,
        "security": security,
        "ddt_eta": ddt_eta,
        "ddt_sell": _ddt_sell_projection(cfg, ddt_eta, next_scan, minutes * 60, now),
        "daily": _daily_info(),
        "running": running or None,
        "events": db.events_list(limit=12),
        "last_check_ok": checks[0].get("severity") == "info" if checks else None,
        "tma_configured": bool(tma_mod.get_steps(cfg, "scan") or tma_mod.get_steps(cfg, "reboot")),
        "tma_base_url": tma_mod.get_base_url(cfg),
        "tma_preset": not (cfg.get("tma", {}).get("steps_reboot") or cfg.get("tma", {}).get("steps_scan")),
        "discovery": {
            "done": bool(discovery),
            "origin": discovery.get("origin"),
            "api_base": (discovery or {}).get("api_base"),
            "ts": (discovery or {}).get("ts"),
        },
    }


# ---------------- HTTP-обработчик ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DoomsdayWeb/1.0"

    def log_message(self, fmt, *args):  # тише в консоль
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- утилиты ---
    def _send_json(self, obj, status=200, cookies=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _body_strict(self):
        """Тело как объект; (None, причина) при невалидном JSON — для настроек
        это ошибка, а не «пустой конфиг» (молчаливое стирание недопустимо)."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}, None
        if n > MAX_BODY:
            return None, f"тело больше {MAX_BODY // 1024} КБ"
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        try:
            obj = json.loads(raw)
        except ValueError as e:
            return None, f"некорректный JSON: {e}"
        if not isinstance(obj, dict):
            return None, "ожидается объект конфига"
        return obj, None

    # --- GET ---
    def do_GET(self):
        cfg = cfgmod.load()
        u = urlparse(self.path)
        path = u.path
        if not _authed(self, cfg):
            return self._send_json({"error": "auth", "msg": "Требуется PIN"}, 401)
        if path == "/" or path == "/index.html":
            return self._send_file(os.path.join(paths.WEB_DIR, "index.html"))
        if path.startswith("/api/"):
            q = parse_qs(u.query)
            return self._api_get(path, q, cfg)
        # статика из web/
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith(".."):
            return self._send_json({"error": "forbidden"}, 403)
        return self._send_file(os.path.join(paths.WEB_DIR, rel))

    def _api_get(self, path, q, cfg):
        if path == "/api/overview":
            return self._send_json(build_overview(cfg))
        if path == "/api/resources-catalog":
            from . import game_data
            return self._send_json({"resources": game_data.catalog()})
        if path == "/api/config":
            out = cfgmod.masked(cfg)
            out["_has_api_hash"] = cfgmod.api_hash_stored()
            return self._send_json(out)
        if path == "/api/events":
            return self._send_json(db.events_list(limit=int(q.get("limit", ["50"])[0]),
                                                  kind=q.get("kind", [""])[0]))
        if path == "/api/runs":
            return self._send_json(db.runs_list(limit=int(q.get("limit", ["20"])[0])))
        if path == "/api/runs/":
            m = db.runs_list(limit=1)
            return self._send_json(m[0] if m else {})
        if path.startswith("/api/runs/"):
            try:
                rid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._send_json({"error": "bad id"}, 400)
            return self._send_json(db.run_get(rid))
        if path == "/api/log":
            n = int(q.get("lines", ["200"])[0])
            try:
                with open(paths.LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except OSError:
                lines = []
            return self._send_json({"lines": [l.rstrip() for l in lines[-n:]]})
        if path == "/api/discovery":
            return self._send_json(db.kv_get("discovery") or {"done": False})
        return self._send_json({"error": "unknown endpoint"}, 404)

    # --- POST/PUT ---
    def do_POST(self):
        cfg = cfgmod.load()
        u = urlparse(self.path)
        if u.path == "/api/login":
            body = self._body()
            pin = str(cfg.get("web", {}).get("pin") or "")
            if pin and secrets.compare_digest(str(body.get("pin", "")), pin):
                cookie = f"ddt_auth={_web_token(cfg)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                return self._send_json({"ok": True}, cookies=[cookie])
            return self._send_json({"ok": False, "msg": "Неверный PIN"}, 403)
        if not _authed(self, cfg):
            return self._send_json({"error": "auth"}, 401)
        if u.path == "/api/config":
            return self._save_config(cfg)
        if u.path == "/api/action":
            return self._action(cfg)
        if u.path == "/api/restart-web":
            # отдельный процесс-перезапускатель: он остановит и НАС (этот
            # обработчик), убьёт все висячие копии панели и поднимет её заново
            try:
                subprocess.Popen([paths.BIN_DOOMSDAY, "web-restart"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return self._send_json({"ok": True, "msg": "Перезапускаю панель…"})
            except OSError as e:
                return self._send_json({"ok": False, "msg": str(e)}, 500)
        return self._send_json({"error": "unknown endpoint"}, 404)

    do_PUT = do_POST

    def _save_config(self, cfg):
        body, err = self._body_strict()
        if err:
            return self._send_json({"error": err}, 400)
        # частичное обновление допустимо: недостающие секции берём из текущего
        # конфига (PUT только exchange не должен стирать schedules/telegram)
        body = cfgmod._deep_merge(cfgmod.load(), body)
        # api_hash: пусто или маска → не трогаем
        tg = body.get("telegram") or {}
        if not tg.get("api_hash") or "•" in str(tg.get("api_hash", "")):
            tg["api_hash"] = (cfg.get("telegram") or {}).get("api_hash", "")
        if str(tg.get("api_id", "")).strip() in ("", "0", "None"):
            tg["api_id"] = (cfg.get("telegram") or {}).get("api_id", 0)
        try:
            tg["api_id"] = int(tg.get("api_id") or 0)
        except (TypeError, ValueError):
            return self._send_json({"error": "api_id должен быть числом"}, 400)
        errs = cfgmod.validate(body)
        if errs:
            return self._send_json({"error": " ".join(errs), "errors": errs}, 400)
        saved = cfgmod.save(body)
        db.event("config", "Настройки сохранены", "через веб-панель")
        return self._send_json({"ok": True, "config": cfgmod.masked(saved)})

    def _action(self, cfg):
        body = self._body()
        action = str(body.get("action") or "").strip()
        allowed = {"reboot", "scan", "summary", "discover", "check", "test-notify",
                   "exchange", "sell-all"}
        if action not in allowed:
            return self._send_json({"error": f"неизвестное действие {action!r}"}, 400)
        rid = spawn_action(action)
        return self._send_json({"ok": True, "run_id": rid, "action": action})


# ---------------- запуск ----------------

# Полночь МСК в epoch мс — по коду игры (use-is-claimed-today: +3ч к UTC,
# затем календарная дата). Фиксированное смещение, не часовой пояс устройства.
MSK_OFFSET_MS = 3 * 3600 * 1000
DAY_MS = 86400 * 1000


def msk_day_index(ms: float) -> int:
    """Номер календарного дня МСК для epoch мс (для сравнения «в тот же день?»)."""
    return int((ms + MSK_OFFSET_MS) // DAY_MS)


def next_msk_midnight_ms(ms: float) -> int:
    """Ближайшая ПОСЛЕДУЮЩАЯ полночь МСК (epoch мс) — момент обновления дня награды."""
    return (msk_day_index(ms) + 1) * DAY_MS - MSK_OFFSET_MS


def _daily_info() -> dict:
    """Секция «daily» для /api/overview: цепочка ежедневных наград.

    Логика — реверс use-is-claimed-today-DlhbwbfL.js: день награды обновляется
    в полночь МСК; если с последнего сбора прошло ≥2 календарных дней МСК —
    цепочка сбрасывается на 1-й день (g>=2 → 1, иначе currentDay+1 с wrap 35→1).
    """
    from . import game_data
    now_ms = time.time() * 1000
    out = {
        "reset_in_sec": max(0, int((next_msk_midnight_ms(now_ms) - now_ms) / 1000)),
        "known": False,
        "claimed_today": None,
        "current_day": None,
        "level": None,
        "next_day": None,
        "next_reward": None,
        "next_reward_short": "",
        "at": None,
    }
    raw = db.kv_get("daily_state")
    if isinstance(raw, dict):
        st = raw
    elif isinstance(raw, str) and raw:
        try:
            st = json.loads(raw)
        except ValueError:
            st = None
    else:
        st = None
    if not isinstance(st, dict):
        return out
    out["known"] = True
    out["current_day"] = st.get("current_day")
    out["level"] = st.get("level")
    out["at"] = st.get("at")
    cur = st.get("current_day") or 1
    last = st.get("last_claimed")
    if isinstance(last, (int, float)) and last > 0:
        gap = msk_day_index(now_ms) - msk_day_index(float(last))
        if gap <= 0:
            out["claimed_today"] = True
            out["next_day"] = cur + 1 if cur < game_data.daily_chain_days() else 1
        elif gap == 1:
            # вчера собирали — сегодня доступен следующий день цепочки
            out["claimed_today"] = False
            out["next_day"] = cur + 1 if cur < game_data.daily_chain_days() else 1
        else:
            # день(и) пропущены — цепочка сбросится на 1-й
            out["claimed_today"] = False
            out["next_day"] = 1
    else:
        out["claimed_today"] = False
        out["next_day"] = cur
    info = game_data.daily_reward(out["next_day"], st.get("level"))
    out["next_reward"] = {k: info[k] for k in ("day", "reward", "name_ru", "amount")}
    out["next_reward_short"] = game_data.fmt_daily_amount(info["reward"], info["amount"])
    return out


def _ddt_sell_projection(cfg: dict, ddt_eta: dict, next_scan, interval_sec: int,
                         now) -> dict:
    """Живая проекция продажи DDT-ресурсов для статусбара («+N time»).

    Снимок производства делает engine.compute_ddt_eta на каждом скане (kv
    «ddt_eta»). Здесь он ПРОИГРЫВАЕТСЯ ВПЕРЁД до текущего момента — той же
    моделью, что симуляция клиента игры (production-controller.runProductionMines):
    партии по workerCount штук завершаются через eta_sec после снимка и далее
    каждые period_real секунд; склад не выше cap. Затем ищется первый скан
    (до 48 вперёд, шаг = интервал скана), на котором выполнится правило
    обмена:
      always — amount = stock - keep >= min;
      cap    — плюс заполнение склада >= threshold_pct.
    Возвращает {rid: {n, t_sec, note}}: n — сколько уйдёт, t_sec — через
    сколько продажа. Это расчёт «когда тикнет DDT»: v2.5.0 показывал eta
    из момента скана без пересчёта и парковался на «00:00:00» между
    часовыми сканами (жалоба 16.09 «стоит 0 часов»).
    """
    from . import game_data

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rules = {}
    ex = cfg.get("exchange") or {}
    if ex.get("enabled", True):
        for rule in ex.get("rules") or []:
            if isinstance(rule, dict) and rule.get("enabled", True):
                rules[str(rule.get("rid") or "")] = rule
    interval_sec = max(60, int(interval_sec or 3600))
    out = {}
    for rid, snap in (ddt_eta or {}).items():
        if not isinstance(snap, dict):
            continue
        if not game_data.SELL_INFO.get(rid, {}).get("is_ddt"):
            continue
        item = {"n": None, "t_sec": None, "note": ""}
        out[rid] = item
        if snap.get("eta_sec") is None:
            item["note"] = snap.get("note") or "нет данных производства"
            continue
        try:
            at = datetime.datetime.fromisoformat(str(snap.get("at")))
        except ValueError:
            item["note"] = "нет данных производства"
            continue
        elapsed = max(0.0, (now - at).total_seconds())
        stock = _num(snap.get("stock")) or 0.0
        cap = _num(snap.get("cap"))
        workers = _num(snap.get("workers")) or 0.0
        eta = _num(snap.get("eta_sec"))
        period = _num(snap.get("period_real"))
        if eta is None or workers <= 0:
            item["note"] = snap.get("note") or "производство не идёт"
            continue

        def stock_at(t):
            """Склад через t сек после снимка (партии уже завершившиеся).
            period_real=None (стоп входов) — растёт только одна идущая партия."""
            done = 0
            if t >= eta:
                done = (int(math.floor((t - eta) / period)) + 1
                        if period and period > 0 else 1)
            s = stock + done * workers
            return min(s, cap) if cap else s

        rule = rules.get(rid)
        if rule is None:
            item["n"] = int(stock_at(elapsed))
            item["note"] = "нет правила обмена — производится, но не продаётся"
            continue
        keep = max(0.0, _num(rule.get("keep")) or 0.0)
        min_amt = max(0.0, _num(rule.get("min")) or 0.0)
        threshold = _num(rule.get("threshold_pct"))
        if threshold is None:
            threshold = 90.0
        mode = str(rule.get("mode") or "cap")

        def sells(s):
            """Сколько уйдёт при складе s (0 — правило не выполнено)."""
            amount = s - keep
            if amount <= 0 or amount < min_amt:
                return 0.0
            if mode == "cap":
                if not cap or s / cap * 100.0 < threshold:
                    return 0.0
            return amount

        # первый скан, на котором продажа состоится
        if next_scan is not None:
            base = (next_scan - at).total_seconds()
            for k in range(48):
                t_k = base + k * interval_sec
                if t_k < elapsed - 1:
                    continue  # этот скан уже прошёл (снимок старше него)
                amt = sells(stock_at(t_k))
                if amt > 0:
                    item["n"] = round(amt, 3)
                    item["t_sec"] = max(0.0, t_k - elapsed)
                    break
        if item["t_sec"] is None:
            s_now = stock_at(elapsed)
            amt = sells(s_now)
            if amt > 0:
                item["n"] = round(amt, 3)
                item["note"] = "скан не запланирован"
            else:
                need = keep + min_amt if mode == "always" else (
                    cap * threshold / 100.0 if cap else None)
                grows = cap is None or stock < cap
                hint = f" — нужно ~{int(math.ceil(need))} шт. на складе" if need else ""
                item["note"] = ("склад заполнен — доход остановился, продажа по правилам"
                                if not grows else
                                "по правилам обмена продажа не состоится" + hint)
                item["n"] = None
        out[rid] = item
    return out


# Порог «cron молчит»: проходы каждые 10 мин + запас. Если дольше — панель
# сама запускает проход (cronie мог умереть; после разморозки Android он не
# всегда оживает, а сервис панели жив и может подстраховать).
CRON_FALLBACK_AFTER_SEC = 660


def _fallback_stale_sec(last_cron_iso, now=None) -> float:
    """Сколько секунд назад был последний проход cron (None — никогда/битое значение)."""
    if not last_cron_iso:
        return None
    try:
        last = datetime.datetime.fromisoformat(str(last_cron_iso))
    except ValueError:
        return None
    now = now or datetime.datetime.now()
    return max(0.0, (now - last).total_seconds())


def _kv_dt(key: str):
    """kv-таймстемп как datetime (None — нет/битое)."""
    raw = db.kv_get(key)
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _cycle_end_watch_due(now=None) -> bool:
    """Точный дожим ребута по концу цикла (диагностика 16.09: цикл истёк в
    20:44, а резервный проход по 11-минутной сетке добрался только в 20:54 —
    производство стояло 10 минут; cronie на устройстве почти мёртв, резерв —
    фактически единственный планировщик).

    True — когда проход нужен СЕЙЧАС, не дожидаясь 11-минутной сетки:
      * до конца цикла < 2 минут (подстраховка окна ребута), или
      * цикл истёк, а успешный ребут для него ещё не сделан
        (last_reboot_ts <= конца цикла). Авральный режим держим 30 минут
        после конца — дальше что-то сломалось глубже, и спамить процессами
        бессмысленно (пусть работает обычная сетка и уведомления).
    Идемпотентно: сам проход всё равно решает через _reboot_due_cycle.
    """
    ends_raw = db.kv_get("passive_farm_ends_at")
    try:
        ends = float(ends_raw) / 1000.0 if ends_raw else None
    except (TypeError, ValueError):
        return False
    if ends is None:
        return False
    now = now or datetime.datetime.now()
    ts = now.timestamp()
    if ends - ts > 120:
        return False  # до конца далеко — окно решает обычная сетка
    if ts > ends + 30 * 60:
        return False  # конец давно — авральный режим выключен
    if ts <= ends:
        return True   # почти конец — подстраховать точное окно ребута
    last_reboot = _kv_dt("last_reboot_ts")
    return not (last_reboot and last_reboot.timestamp() > ends)


def _cron_fallback_loop() -> None:
    """Резервный планировщик в процессе веб-панели.

    Случай 16.09 07:04: окно авто-ребута пришлось на замороженный Android'ом
    Termux — проходы cron не выполнялись, ребут не случился, производство
    простояло. cronie живёт отдельным процессом и умирает независимо от
    сервиса панели; после оттайки телефона никто его не перезапускает.
    Здесь панель (runit следит за ней и перезапускает при падении) каждые
    30 секунд смотрит на last_cron_ts: если проходов не видно дольше
    CRON_FALLBACK_AFTER_SEC — сама запускает `doomsday cron`. Каждый проход
    (в т.ч. резервный и ночной) обновляет last_cron_ts, поэтому при живом
    cronie резерв никогда не срабатывает и дублей не бывает (плюс блокировка
    SingleInstance в самом воркере — неблокирующая, лишний выход мгновенен).

    17.09: добавлен точный дожим по концу цикла (_cycle_end_watch_due) —
    11-минутная сетка при мёртвом cronie опаздывала к концу цикла на
    10 минут. Плюс резервные проходы теперь пишут план в cron.log с
    меткой [fallback] (раньше stdout уходил в /dev/null — диагностика
    проходила вслепую).
    """
    while True:
        time.sleep(30)
        try:
            cfg = cfgmod.load()
            if not (cfg.get("web") or {}).get("cron_fallback", True):
                continue
            stale = _fallback_stale_sec(db.kv_get("last_cron_ts"))
            due = ((stale is None or stale > CRON_FALLBACK_AFTER_SEC)
                   or _cycle_end_watch_due())
            if not due:
                continue
            # анти-шторм: что бы ни случилось, не чаще одного спавна в минуту
            # (если проход умирает до записи last_cron_ts, не плодим процессы)
            last_spawn = _fallback_stale_sec(db.kv_get("last_cron_fallback_spawn"))
            if last_spawn is not None and last_spawn < 60:
                continue
            db.kv_set("last_cron_fallback_spawn", db.now_iso())
            env = dict(os.environ, DOOMSDAY_CRON_FALLBACK="1")
            try:
                out = open(paths.CRON_LOG_PATH, "a", encoding="utf-8")
            except OSError:
                out = subprocess.DEVNULL
            subprocess.Popen(["sh", paths.BIN_DOOMSDAY, "cron"], cwd=paths.APP_DIR,
                             stdout=out, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=env)
            if out is not subprocess.DEVNULL:
                out.close()
            # событие — не чаще раза в час, чтобы не засорять журнал
            note_age = _fallback_stale_sec(db.kv_get("last_cron_fallback_note"))
            if note_age is None or note_age > 3600:
                db.kv_set("last_cron_fallback_note", db.now_iso())
                why = "конец цикла производства" if _cycle_end_watch_due() else "cronie не жив?"
                db.event("web", "Резервный проход cron",
                         "Проходы cron не видны дольше "
                         f"{CRON_FALLBACK_AFTER_SEC // 60} мин либо подошёл конец цикла "
                         f"({why}) — панель запустила проход сама.")
        except Exception as e:  # поток живёт вечно и не должен падать
            try:
                log.warning("Резервный планировщик: %s", e)
            except Exception:
                pass


def serve_forever(cfg: dict) -> None:
    host = cfg.get("web", {}).get("host", "127.0.0.1")
    port = int(cfg.get("web", {}).get("port", 8080) or 8080)
    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=_cron_fallback_loop, daemon=True,
                     name="cron-fallback").start()
    pin = " (PIN включён)" if cfg.get("web", {}).get("pin") else ""
    print(f"Веб-панель: http://{host}:{port}{pin}")
    db.event("web", "Веб-панель запущена", f"http://{host}:{port}")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
