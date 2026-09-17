#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Интеграционный тест _auto_exchange: мок tma.run_steps, проверка отчётов,
балансов kv, обновления ресурсов, уведомлений и ветки ошибок.

v2.4.3: цифры отчёта — только фактические (продано = count − resourceCount),
частичная продажа добирается повторными sellItem, выручка — из дельты
баланса ответа. Проверяем все эти сценарии."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, "/home/z/my-project/doomsday-bot")
os.environ["DOOMSDAY_BOT_DIR"] = "/home/z/my-project/doomsday-bot"

from lib import paths, config as cfgmod, db, engine, tma

tmp = tempfile.mkdtemp()
paths.DB_PATH = os.path.join(tmp, "test.db")
paths.CONFIG_PATH = os.path.join(tmp, "config.json")
db._conn = None

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok' if cond else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    if not cond:
        fails.append(name)


calls = []


def _ok_out(ctx, step):
    return {"ok": True, "results": [{"name": step[0]["name"], "status": "ok",
                                     "detail": "HTTP 200"}], "ctx": ctx}


def fake_run_steps(steps, ctx, timeout=25):
    """Мок: sellItem возвращает новые балансы и остаток."""
    body = steps[0]["body"]["data"]
    calls.append((body["resourceId"], body["amount"]))
    out_ctx = dict(ctx)
    if body["resourceId"] == "cassete":
        out_ctx.update({"sell_mcoin": 5, "sell_coin": 21_000_000_000,
                        "sell_left": 100, "sell_raw": '{"result":{}}'})
    else:  # floppy
        out_ctx.update({"sell_mcoin": 5, "sell_coin": 36_000_000_000,
                        "sell_left": 0, "sell_raw": '{"result":{}}'})
    return _ok_out(out_ctx, steps)


def fake_partial(steps, ctx, timeout=25):
    """Мок частичной продажи: первый вызов списывает 38000 из 58000,
    второй — добирает остаток до нуля (сценарий 15.09 20:05)."""
    body = steps[0]["body"]["data"]
    calls.append((body["resourceId"], body["amount"]))
    out_ctx = dict(ctx)
    if len([c for c in calls if c[0] == "cassete"]) == 1:
        out_ctx.update({"sell_mcoin": 5, "sell_coin": 1000 + 38000 * 6144,
                        "sell_left": 20000,
                        "sell_raw": '{"result":{"resourceCount":20000}}'})
    else:
        out_ctx.update({"sell_mcoin": 5, "sell_coin": 1000 + 58000 * 6144,
                        "sell_left": 0, "sell_raw": '{"result":{}}'})
    return _ok_out(out_ctx, steps)


def fake_nop(steps, ctx, timeout=25):
    """Мок «сервер ничего не списал»: 200 OK, остаток равен складу."""
    body = steps[0]["body"]["data"]
    calls.append((body["resourceId"], body["amount"]))
    out_ctx = dict(ctx)
    out_ctx.update({"sell_mcoin": 5, "sell_coin": 1000, "sell_left": 58000,
                    "sell_raw": '{"result":{"resourceCount":58000,"coin":1000}}'})
    return _ok_out(out_ctx, steps)


def fake_run_steps_fail(steps, ctx, timeout=25):
    body = steps[0]["body"]["data"]
    calls.append((body["resourceId"], body["amount"]))
    return {"ok": False, "results": [{"name": steps[0]["name"], "status": "fail",
                                      "detail": "HTTP 500: boom"}], "ctx": dict(ctx)}


sent = []
orig_notify = engine.notify_mod.notify
engine.notify_mod.notify = lambda cfg, kind, title, text, **kw: sent.append((kind, title, text)) or True
orig_notify_error = engine.notify_mod.notify_error
engine.notify_mod.notify_error = lambda cfg, title, text: sent.append(("errors", title, text)) or True
orig_pause = engine._sell_pause
engine._sell_pause = lambda cfg, seed: None  # без реальных пауз

resources = [
    {"name": "Дискета", "id": "floppy", "current": 29000, "max": 31000, "pct": 93.5, "state": ""},
    {"name": "Кассета", "id": "cassete", "current": 58000, "max": 58000, "pct": 100.0,
     "state": "склад переполнен"},
    {"name": "Жёсткий диск", "id": "hdd", "current": 400, "max": 10500, "pct": 3.8, "state": ""},
]

cfg = cfgmod._deep_merge(cfgmod.DEFAULTS, {"exchange": {"rules": [
    {"rid": "floppy", "mode": "cap", "threshold_pct": 90, "keep": 0, "min": 1, "enabled": True},
    {"rid": "hdd", "mode": "cap", "threshold_pct": 90, "keep": 0, "min": 1, "enabled": True},
    {"rid": "cassete", "mode": "cap", "threshold_pct": 90, "keep": 100, "min": 1, "enabled": True},
]}})

print("test: _auto_exchange — успех")
engine.tma.run_steps = fake_run_steps
reports = asyncio.run(engine._auto_exchange(cfg, resources, {"init_data": "x"}, 25))
check("два ресурса проданы (hdd ниже порога не тронут)", len(reports) == 2, str(calls))
check("порядок и объёмы продаж", calls == [("floppy", 29000), ("cassete", 57900)], str(calls))
check("отчёты содержат выручку", all("proceeds" in r for r in reports), str(reports)[:120])
check("в отчётах есть запрошенное количество",
      all("requested" in r for r in reports), str(reports)[:120])
check("продано = запрошено (полная продажа)",
      all(r["amount"] == r["requested"] for r in reports), str(reports)[:160])
check("балансы записаны в kv (последней продажи)", db.kv_get("balance_coin") == 21000000000
      and db.kv_get("balance_mcoin") == 5, str(db.kv_get("balance_coin")))
by_id = {r["id"]: r for r in resources}
check("ресурсы обновлены на месте", by_id["floppy"]["current"] == 0
      and by_id["cassete"]["current"] == 100, f"{by_id['floppy']['current']}/{by_id['cassete']['current']}")
check("переполнение сброшено", by_id["cassete"]["state"] == "", by_id["cassete"]["state"])
check("pct пересчитан", by_id["cassete"]["pct"] == round(100 / 58000 * 100, 1),
      str(by_id["cassete"]["pct"]))
kinds = [k for k, _t, _b in sent]
check("push об обмене отправлен", "exchange_report" in kinds, str(sent))
evs = db.events_list(limit=10, kind="exchange")
check("события обмена в журнале", len(evs) == 2, str(len(evs)))

print("test: _auto_exchange — частичная продажа добирается повтором (15.09 20:05)")
calls.clear()
sent.clear()
engine.tma.run_steps = fake_partial
res3 = [{"name": "Кассета", "id": "cassete", "current": 58000, "max": 58000, "pct": 100.0,
         "state": "склад переполнен"}]
reports3 = asyncio.run(engine._auto_exchange(cfg, res3, {"balance_coin": 1000}, 25))
check("два вызова sellItem (добор остатка)",
      calls == [("cassete", 57900), ("cassete", 19900)], str(calls))
check("продано СУММАРНО по факту", reports3 and reports3[0]["amount"] == 58000,
      str(reports3)[:200])
check("attempts = 2", reports3 and reports3[0]["attempts"] == 2, str(reports3)[:160])
check("выручка из дельты баланса (не оценка)",
      reports3 and reports3[0]["proceeds_real"] is True, str(reports3)[:160])
check("ресурс обнулён после добора", res3[0]["current"] == 0, str(res3[0]["current"]))
evs3 = db.events_list(limit=5, kind="exchange")
check("событие полного итога без warn",
      evs3 and evs3[0]["severity"] != "warn", str(evs3[0]["severity"]) if evs3 else "-")

print("test: _auto_exchange — сервер ничего не списал (200, остаток = складу)")
calls.clear()
sent.clear()
engine.tma.run_steps = fake_nop
res4 = [{"name": "Кассета", "id": "cassete", "current": 58000, "max": 58000, "pct": 100.0,
         "state": "склад переполнен"}]
reports4 = asyncio.run(engine._auto_exchange(cfg, res4, {"balance_coin": 1000}, 25))
check("повтор не спамится (нет прогресса — 1 вызов)", len(calls) == 1, str(calls))
check("продано честно 0", reports4 and reports4[0]["amount"] == 0, str(reports4)[:200])
check("выручки нет", reports4 and reports4[0]["proceeds"] == "нет", str(reports4)[:160])
evs4 = db.events_list(limit=5, kind="exchange")
check("событие warn «обмен не прошёл»",
      evs4 and evs4[0]["severity"] == "warn" and "не прошёл" in evs4[0]["title"],
      str(evs4[0]["title"]) if evs4 else "-")
check("в событии сырой ответ сервера",
      evs4 and "ответ сервера" in (evs4[0]["body"] or ""), (evs4[0]["body"] or "")[:120] if evs4 else "-")
notif = [t for k, _t, t in sent if k == "exchange_report"]
check("push честно показывает ×0 и недобор",
      notif and "×0" in notif[-1] and "меньше запрошенного" in notif[-1],
      (notif[-1] if notif else "")[:160])

print("test: _auto_exchange — ошибка API")
calls.clear()
sent.clear()
engine.tma.run_steps = fake_run_steps_fail
res2 = [{"name": "Дискета", "id": "floppy", "current": 29000, "max": 31000, "pct": 93.5, "state": ""}]
reports2 = asyncio.run(engine._auto_exchange(cfg, res2, {}, 25))
check("отчётов нет", reports2 == [], str(reports2))
check("уведомление об ошибке", any(k == "errors" for k, _t, _b in sent), str(sent)[:100])
ev_err = db.events_list(limit=5, kind="exchange")
check("ошибка в журнале с severity critical",
      any(e["severity"] == "critical" for e in ev_err), "")

print("test: _auto_exchange — force_all («собрать всю память»)")
calls.clear()
sent.clear()
engine.tma.run_steps = fake_run_steps
# склады ниже порогов + автообмен ВЫКЛЮЧЕН — форс-кнопка всё равно продаёт
cfg_off = cfgmod._deep_merge(cfgmod.DEFAULTS, {"exchange": {"enabled": False}})
res_f = [
    {"name": "Дискета", "id": "floppy", "current": 5250, "max": 31000, "pct": 16.9, "state": ""},
    {"name": "Жёсткий диск", "id": "hdd", "current": 7000, "max": 10500, "pct": 66.7, "state": ""},
    {"name": "Кассета", "id": "cassete", "current": 45820, "max": 58000, "pct": 79.0, "state": ""},
    {"name": "Урановые таблетки", "id": "uran_pills", "current": 5, "max": 24, "pct": 20.8,
     "state": ""},
]
reports_f = asyncio.run(engine._auto_exchange(cfg_off, res_f, {"init_data": "x"}, 25, force_all=True))
sold_ids = sorted(set(c[0] for c in calls))
first_ask = {}
for rid, amt in calls:
    first_ask.setdefault(rid, amt)
check("проданы ВСЕ байтовые носители (мимо порогов и выключателя)",
      sold_ids == ["cassete", "floppy", "hdd"], str(calls))
check("запрошены полные склады (keep=0)",
      first_ask.get("floppy") == 5250 and first_ask.get("cassete") == 45820
      and first_ask.get("hdd") == 7000, str(first_ask))
check("uran_pills (DDT) в форс-продажу не попал", "uran_pills" not in sold_ids, str(sold_ids))
check("три отчёта", len(reports_f) == 3, str(len(reports_f)))
by_f = {r["id"]: r for r in res_f}
check("склады обнулены/обновлены по ответу",
      by_f["floppy"]["current"] == 0 and by_f["cassete"]["current"] == 100,
      f"{by_f['floppy']['current']}/{by_f['cassete']['current']}")
titles_f = [t for k, t, _b in sent if k == "exchange_report"]
check("push озаглавлен «Память собрана (вручную)»",
      titles_f and titles_f[-1] == "Память собрана (вручную)", str(titles_f))
evs_f = db.events_list(limit=6, kind="exchange")
check("в событиях режим «вручную (вся память)»",
      any("вручную (вся память)" in (e["body"] or "") for e in evs_f),
      str([e["body"][:60] for e in evs_f])[:200])

print("test: force_all — пустые байтовые склады: продажа не нужна")
calls.clear()
sent.clear()
res_empty = [
    {"name": "Дискета", "id": "floppy", "current": 0, "max": 31000, "pct": 0, "state": ""},
    {"name": "Урановые таблетки", "id": "uran_pills", "current": 7, "max": 24, "pct": 29.2,
     "state": ""},
]
reports_e = asyncio.run(engine._auto_exchange(cfg_off, res_empty, {"init_data": "x"}, 25,
                                              force_all=True))
check("вызовов sellItem нет (продавать нечего)", calls == [], str(calls))
check("отчётов нет, уран не тронут", reports_e == [], str(reports_e))

print("test: _exchange_plan force_all — чистая функция")
res_fresh = [dict(r) for r in res_f]
for r in res_fresh:  # res_f мутирован предыдущей продажей — восстанавливаем склады
    if r["id"] == "floppy":
        r["current"] = 5250
    if r["id"] == "cassete":
        r["current"] = 45820
    if r["id"] == "hdd":
        r["current"] = 7000
plan_f = engine._exchange_plan(cfg_off, res_fresh, force_all=True)
pf = {r: a for r, a, _rule in plan_f}
check("план: все носители с текущим складом",
      pf == {"cassete": 45820, "floppy": 5250, "hdd": 7000}, str(pf))
check("план правилами остаётся пуст при выключенном обмене",
      engine._exchange_plan(cfg_off, res_f) == [], "")

engine.notify_mod.notify = orig_notify
engine.notify_mod.notify_error = orig_notify_error
engine._sell_pause = orig_pause

print()
if fails:
    print("ПРОВАЛЕНО:", fails)
    sys.exit(1)
print("Все проверки пройдены ✔")
