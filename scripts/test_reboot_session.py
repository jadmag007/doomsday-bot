#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Интеграционный тест action_reboot против мок-сервера игры.

Мок воспроизводит семантику сессий, обнаруженную 2026-09-15 в логах устройства
(403 PERMISSION_DENIED "Expired session", внутренний статус 418):
  - initUser РЕГИСТРИРУЕТ hash сессии (первый вызов веб-приложения при загрузке);
  - rebootProduction с незарегистрированным/чужим hash → 403 Expired session.

Проверяем:
  A) основной баг: ребут с незнакомой сессией отклонён → новый код (initUser
     перед rebootProduction) проходит;
  B) свежий цикл (кто-то перезапустил) → rebootProduction НЕ вызывается;
  C) перехват сессии (игра открыта на телефоне между initUser и reboot) →
     один повтор с новым webview;
  D) уведомления: успех / «Ребут не понадобился»;
  E) регрессия 15.09 19:15: после ребута панель и автообмен обновляются
     сразу (повторный initUser → снимок ресурсов → sellItem по правилам,
     контрольный скан назначен) — без ожидания скана по интервалу.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/home/z/my-project/doomsday-bot")
os.environ["DOOMSDAY_BOT_DIR"] = "/home/z/my-project/doomsday-bot"

from lib import paths, config as cfgmod, db, engine
import lib.tgapi as tgapi_mod
import lib.notify as notify_mod

tmp = tempfile.mkdtemp()
paths.DB_PATH = os.path.join(tmp, "test.db")
paths.CONFIG_PATH = os.path.join(tmp, "config.json")
db._conn = None

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok' if cond else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---------------- мок-сервер игры (Firebase callable) ----------------

state = {
    "registered": None,       # hash последней зарегистрированной сессии
    "ends_at": 0,             # конец цикла производства (мс)
    "init_calls": 0,
    "reboot_calls": 0,
    "steal_session_once": False,  # имитация: игра перехватила сессию
    "cassete_count": 100,     # склад кассеты (для автообмена после ребута)
    "sell_calls": [],         # журнал sellItem
}


def _mines():
    """Шахты в ответе initUser: кассета с заданным складом."""
    return {
        "cassete": {"levelStore": 29, "store": {"count": state["cassete_count"]},
                    "usagePerMinute": 0,
                    "passive": {"workerCount": 2, "craftPerMinute": 30,
                                "craftPerMinuteReal": 30, "progress": 0}},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тише
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(ln) or b"{}").get("data") or {}
        sess = data.get("session") or ""
        if self.path.endswith("/initUser"):
            state["init_calls"] += 1
            state["registered"] = sess  # регистрация сессии
            self._send(200, {"result": {
                "isPremium": True,
                "passiveFarm": {"active": True, "endsAt": state["ends_at"]},
                "gameStats": {"coin": 1000, "mCoin": 7, "mines": _mines()},
            }})
            return
        if self.path.endswith("/rebootProduction"):
            state["reboot_calls"] += 1
            if state["steal_session_once"]:
                # игра на телефоне перехватила сессию после нашего initUser:
                # сервер считает актуальной другую, а наша «истекла»
                state["steal_session_once"] = False
                state["registered"] = "stolen-by-phone"
                self._send(403, {"error": {"details": {"status": 418},
                                           "message": "Expired session",
                                           "status": "PERMISSION_DENIED"}})
                return
            if sess != state["registered"]:
                # ровно то, что ловил бот 2026-09-15 в 18:37
                self._send(403, {"error": {"details": {"status": 418},
                                           "message": "Expired session",
                                           "status": "PERMISSION_DENIED"}})
                return
            state["ends_at"] = int((time.time() + 12 * 3600) * 1000)
            self._send(200, {"result": {"active": True,
                                        "startedAt": int(time.time() * 1000),
                                        "endsAt": state["ends_at"]}})
            return
        if self.path.endswith("/sellItem"):
            if sess != state["registered"]:
                self._send(403, {"error": {"details": {"status": 418},
                                           "message": "Expired session",
                                           "status": "PERMISSION_DENIED"}})
                return
            state["sell_calls"].append((data.get("resourceId"), data.get("amount")))
            if data.get("resourceId") == "cassete":
                state["cassete_count"] = 0  # keep=0 → продано всё
            self._send(200, {"result": {"mCoin": 7, "coin": 1000,
                                        "resourceCount": state["cassete_count"]}})
            return
        self._send(404, {"error": {"message": "not found"}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------------- заглушки Telegram ----------------

WEBVIEW_TPL = ("https://telegram-miracle-f1779.web.app/#tgWebAppData="
               "user%3D%257B%2522id%2522%253A1%257D%26auth_date%3D1700000000"
               "%26hash%3D{h}")
webview_q = []


class FakeClient:
    async def disconnect(self):
        pass


async def fake_connect(cfg, interactive=False):
    return FakeClient()


async def fake_get_bot(client, cfg):
    return object()


async def fake_send_start(client, bot, text="/start"):
    return None


async def fake_resolve(client, bot, cfg):
    return webview_q.pop(0)


tgapi_mod.connect = fake_connect
tgapi_mod.get_bot = fake_get_bot
tgapi_mod.send_start = fake_send_start
tgapi_mod.resolve_webview_url = fake_resolve

sent = []
notify_mod.notify = lambda cfg, kind, title, text, **kw: sent.append((kind, title)) or True
notify_mod.notify_error = lambda cfg, title, text: sent.append(("errors", title)) or True

# ускоряем ретрай (без реальной паузы 3 с)
engine.asyncio.sleep = lambda *_a, **_k: _fake_sleep()


async def _fake_sleep():
    return None


cfg = cfgmod._deep_merge(cfgmod.DEFAULTS, {
    "tma": {"base_url": f"http://127.0.0.1:{PORT}"},
    "schedules": {"reboot_before_end_minutes": 10},
    "notify": {"events": {"reboot_report": True}},
})


def reset(ends_at_ms, hashes, steal=False):
    state.update(registered=None, ends_at=ends_at_ms, init_calls=0,
                 reboot_calls=0, steal_session_once=steal,
                 cassete_count=100, sell_calls=[])
    webview_q.clear()
    webview_q.extend(WEBVIEW_TPL.format(h=h) for h in hashes)
    sent.clear()
    db.kv_set("passive_farm_ends_at", "0")
    db.kv_set("notified_reboot_needed", [])
    db.kv_set("force_scan_at", None)


def run_reboot():
    return asyncio.run(engine.action_reboot(cfg))


# ---------------- A) основной баг: сессия без initUser отклоняется ----------------
print("test A: ребут после initUser (баг «Expired session» 2026-09-15)")
reset(int((time.time() - 46 * 60) * 1000), ["H1"])  # цикл истёк 46 минут назад
r = run_reboot()
steps = {s["name"]: s["status"] for s in r["steps"]}
check("ребут успешен", r["ok"] is True, json.dumps(r["steps"], ensure_ascii=False))
check("initUser выполнен перед rebootProduction", steps.get("initUser") == "ok", "")
check("rebootProduction выполнен", steps.get("rebootProduction") == "ok", "")
check("сервер видел ровно один ребут", state["reboot_calls"] == 1, str(state["reboot_calls"]))
check("новый дедлайн цикла сохранён",
      db.kv_get("passive_farm_ends_at") == str(state["ends_at"]),
      f"kv={db.kv_get('passive_farm_ends_at')} srv={state['ends_at']}")
check("уведомление «перезапущено»",
      ("reboot_report", "Производство перезапущено") in sent, str(sent))

# контроль регрессии: старый код (только rebootProduction, без initUser)
# на этом моке падает — мок не принимает незнакомый hash
print("test A2: мок действительно отклоняет незнакомую сессию (регрессия)")
reset(int((time.time() - 46 * 60) * 1000), ["H1"])
state["registered"] = "SOMEONE-ELSE"  # имитируем: initUser не вызван
r2 = run_reboot()
# наш initUser(H1) перерегистрирует — поэтому напрямую: вызов reboot без init
outcome = engine.tma.run_steps(
    [s for s in engine.tma.get_steps(cfg, "reboot") if s["name"] == "rebootProduction"],
    engine.tma.build_context(cfg, WEBVIEW_TPL.format(h="H9")))
check("rebootProduction без initUser → fail (мок строг)",
      outcome["ok"] is False and "Expired" in outcome["results"][0]["detail"],
      outcome["results"][0]["detail"][:80])

# ---------------- B) свежий цикл → ребут не нужен ----------------
print("test B: цикл уже свежий — rebootProduction не вызывается")
reset(int((time.time() + 11 * 3600) * 1000), ["H2"])
r = run_reboot()
steps = {s["name"]: s["status"] for s in r["steps"]}
check("итог ok", r["ok"] is True, "")
check("rebootProduction пропущен (skip)", steps.get("rebootProduction") == "skip", str(steps))
check("сервер ребут НЕ получал", state["reboot_calls"] == 0, str(state["reboot_calls"]))
check("kv обновлён свежим циклом",
      db.kv_get("passive_farm_ends_at") == str(state["ends_at"]), "")
check("уведомление «Ребут не понадобился»",
      ("reboot_report", "Ребут не понадобился") in sent, str(sent))

# ---------------- C) перехват сессии → повтор с новым webview ----------------
print("test C: сессию перехватила игра на телефоне — повтор")
reset(int((time.time() - 5 * 60) * 1000), ["H3", "H4"], steal=True)
r = run_reboot()
steps = {s["name"]: s["status"] for s in r["steps"]}
notes = " ".join(r["notes"])
check("после повтора ребут успешен", r["ok"] is True,
      json.dumps(r["steps"], ensure_ascii=False))
check("повтор отмечен в notes", "повтор" in notes or "сессия" in notes, notes)
check("первая (провальная) попытка в журнале",
      any(s["status"] == "fail" and "Expired" in s["detail"] for s in r["steps"]),
      str([s["detail"][:60] for s in r["steps"] if s["status"] == "fail"]))
check("rebootProduction ok", steps.get("rebootProduction") == "ok", "")
check("два webview израсходованы", len(webview_q) == 0, str(webview_q))
check("новый дедлайн сохранён",
      db.kv_get("passive_farm_ends_at") == str(state["ends_at"]), "")

# ---------------- D) истинный отказ (не сессия) → без повтора ----------------
print("test D: другая ошибка — ретрая нет, уведомление об ошибке")
reset(int((time.time() - 5 * 60) * 1000), ["H5"])


class Handler500(Handler):
    def do_POST(self):
        if self.path.endswith("/initUser"):
            self._send(500, {"error": {"message": "boom"}})
            return
        self._send(500, {"error": {"message": "boom"}})


srv2 = ThreadingHTTPServer(("127.0.0.1", 0), Handler500)
cfg2 = cfgmod._deep_merge(cfg, {"tma": {"base_url": f"http://127.0.0.1:{srv2.server_address[1]}"}})
threading.Thread(target=srv2.serve_forever, daemon=True).start()
r = asyncio.run(engine.action_reboot(cfg2))
check("ребут провален", r["ok"] is False, "")
check("initUser fail", {s["name"]: s["status"] for s in r["steps"]}.get("initUser") == "fail", "")
check("уведомление об ошибке", ("errors", "Ребут не удался") in sent, str(sent))

# ------------- E) после ребута панель и обмен обновляются сразу -------------
print("test E: после ребута — снимок, автообмен и контрольный скан (регрессия 19:15)")
reset(int((time.time() - 46 * 60) * 1000), ["H6"])
state["cassete_count"] = 52180  # 52180/58000 = 90.0% — порог cap 90%
r = run_reboot()
check("ребут успешен", r["ok"] is True, json.dumps(r["steps"], ensure_ascii=False))
check("повторный initUser после ребута (снимок по свежему циклу)",
      state["init_calls"] == 2, str(state["init_calls"]))
check("sellItem вызван для кассеты сразу после ребута",
      state["sell_calls"] == [("cassete", 52180)], str(state["sell_calls"]))
check("отчёт обмена в результате ребута",
      any(x.get("rid") == "cassete" for x in (r.get("exchange") or [])),
      str(r.get("exchange")))
_rows = {x.get("id"): x for x in db.resources_latest_ru()}
check("снимок обновлён: статус «требуется ребут» снят",
      (_rows.get("cassete") or {}).get("state") == "", str(_rows.get("cassete")))
check("снимок обновлён: склад после продажи",
      (_rows.get("cassete") or {}).get("current") == 0, str(_rows.get("cassete")))
check("контрольный скан назначен", db.kv_get("force_scan_at") is not None,
      str(db.kv_get("force_scan_at")))
check("уведомление об автообмене",
      ("exchange_report", "Автообмен выполнен") in sent, str(sent))

srv.shutdown()
srv2.shutdown()
print()
if fails:
    print("ПРОВАЛЫ: " + ", ".join(fails))
    sys.exit(1)
print("Все проверки пройдены ✔")
