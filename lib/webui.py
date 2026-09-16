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
import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
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

def serve_forever(cfg: dict) -> None:
    host = cfg.get("web", {}).get("host", "127.0.0.1")
    port = int(cfg.get("web", {}).get("port", 8080) or 8080)
    httpd = ThreadingHTTPServer((host, port), Handler)
    pin = " (PIN включён)" if cfg.get("web", {}).get("pin") else ""
    print(f"Веб-панель: http://{host}:{port}{pin}")
    db.event("web", "Веб-панель запущена", f"http://{host}:{port}")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
