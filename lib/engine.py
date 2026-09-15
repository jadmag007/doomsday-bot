# -*- coding: utf-8 -*-
"""Высокоуровневые действия: скан ресурсов, ребут производства, сводка, диагностика."""
import asyncio
import json
import logging
import re

from . import config as cfgmod
from . import db
from . import notify as notify_mod
from . import tgapi
from . import tma

log = logging.getLogger("doomsday.engine")


# ---------------- парсеры ----------------

def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def parse_resources_from_json(parsed, res_cfg: dict) -> list:
    """Ресурсы из JSON-ответа API по json_path + fields."""
    out = []
    path = (res_cfg.get("json_path") or "").strip()
    items = tma.json_path(parsed, path) if path else parsed
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return out
    fields = res_cfg.get("fields") or {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get(fields.get("name", "name")) or "?").strip()
        cur = _num(it.get(fields.get("current", "amount")))
        mx = _num(it.get(fields.get("max", "capacity")))
        state = str(it.get(fields.get("state", "state")) or "")
        if name and (cur is not None or mx is not None or state):
            out.append(_mk_resource(name, cur, mx, state))
    return out


def parse_resources_from_text(text: str, patterns: list) -> list:
    """Ресурсы из текста (бот-чат/ответ API) по regex-паттернам: имя, текущее, максимум."""
    out = []
    for p in patterns or []:
        if not isinstance(p, dict) or not p.get("enabled", True):
            continue
        rx = p.get("regex") or ""
        try:
            for m in re.finditer(rx, text or "", re.I | re.U):
                g = m.groups()
                name = (p.get("name") if p.get("name") and len(g) < 2 else None) or (g[0] if g else p.get("name"))
                cur = _num(g[1]) if len(g) > 1 else None
                mx = _num(g[2]) if len(g) > 2 else None
                if name:
                    out.append(_mk_resource(str(name).strip(), cur, mx, p.get("state", "")))
        except re.error:
            log.warning("Некорректный regex в паттерне ресурсов: %r", rx)
    # дедуп по имени — остаётся последнее вхождение
    dedup = {}
    for r in out:
        dedup[r["name"]] = r
    return list(dedup.values())


def _mk_resource(name, cur, mx, state="", rid=None):
    pct = None
    if cur is not None and mx is not None and mx > 0:
        pct = round(cur / mx * 100, 1)
    row = {"name": name[:64], "current": cur, "max": mx, "pct": pct, "state": state or ""}
    if rid:
        row["id"] = rid
    return row


def _res_key(r: dict) -> str:
    """Стабильный ключ ресурса: игровой id либо имя (для кастомных парсеров)."""
    from . import game_data
    rid = r.get("id") or game_data.rid_by_name(r.get("name"))
    return rid or str(r.get("name") or "")


def parse_chat_alerts(messages: list, patterns: list) -> list:
    """Уведомления бота в чате, совпавшие с паттернами (catch-up мониторинг)."""
    alerts = []
    for m in messages or []:
        for p in patterns or []:
            if not isinstance(p, dict):
                continue
            try:
                if re.search(p.get("match") or "", m.get("text") or "", re.I | re.U):
                    alerts.append({
                        "msg_id": m.get("id"),
                        "date": m.get("date"),
                        "title": p.get("title") or "Уведомление игры",
                        "text": (m.get("text") or "")[:500],
                        "severity": p.get("severity", "warn"),
                        "notify": bool(p.get("notify", True)),
                    })
                    break
            except re.error:
                continue
    return alerts


# ---------------- парсер состояния Doomsday Tyranny ----------------

def parse_resources_doomsday(state: dict) -> list:
    """Ресурсы из ответа initUser игры Doomsday Tyranny.

    Структура (реверс web-приложения): gameStats.mines[resourceId] содержит
    passive.{workerCount,isPaused,craftPerMinute,craftPerMinuteReal},
    store.count, levelStore, usagePerMinute; passiveFarm.endsAt (epoch мс) —
    конец текущего цикла производства. Ёмкости складов — из статической
    таблицы игры (lib/game_data.py). Состояния считаем той же логикой,
    что и интерфейс игры (idle → требуется ребут → пауза → дефициты → полон).
    """
    from . import game_data
    import time as _time

    if not isinstance(state, dict):
        return []
    mines = ((state.get("gameStats") or {}).get("mines")) or {}
    if not isinstance(mines, dict):
        return []
    farm = state.get("passiveFarm") or {}
    ends_at = farm.get("endsAt") or 0
    farm_active = isinstance(ends_at, (int, float)) and ends_at >= _time.time() * 1000

    rows = []
    for rid, mine in mines.items():
        if not isinstance(mine, dict) or not isinstance(mine.get("passive"), dict):
            continue  # шахта ещё не открыта
        passive = mine["passive"]
        store = mine.get("store") or {}
        count = _num(store.get("count"))
        workers = _num(passive.get("workerCount")) or 0
        caps = game_data.CAPACITIES.get(rid) or []
        cap = None
        if caps:
            level = mine.get("levelStore")
            idx = level if isinstance(level, int) and 0 <= level < len(caps) else (1 if len(caps) > 1 else 0)
            cap = float(caps[idx])
        craft = _num(passive.get("craftPerMinute"))
        craft_real = _num(passive.get("craftPerMinuteReal"))
        usage = _num(mine.get("usagePerMinute"))
        # порядок проверок повторяет логику интерфейса игры
        if workers == 0:
            st = "простаивает"
        elif not farm_active:
            st = "требуется ребут"
        elif passive.get("isPaused"):
            st = "пауза"
        elif craft_real is not None and craft is not None and craft_real < craft:
            st = "нехватка компонентов"
        elif craft is not None and usage is not None and craft < usage:
            st = "дефицит"
        elif cap is not None and count is not None and count >= cap:
            st = "склад переполнен"
        else:
            st = ""
        name = game_data.ru_name(rid, rid)
        row = _mk_resource(name, count, cap, st, rid)
        # данные производства (для расчёта времени до следующей единицы —
        # ETA DDT-ресурсов в статусбаре): progress — СЕКУНДЫ текущего крафта
        row["craft_info"] = {
            "progress": _num(passive.get("progress")) or 0.0,
            "craft": craft,
            "craft_real": craft_real,
            "workers": workers or 0,
        }
        rows.append(row)
    return rows


def _fmt_ms_deadline(ends_at_ms) -> str:
    """Человекочитаемый дедлайн цикла из epoch-миллисекунд."""
    if not isinstance(ends_at_ms, (int, float)):
        return "?"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ends_at_ms / 1000).strftime("%d.%m %H:%M")


# ---------------- автообмен (sellItem) ----------------

def _exchange_plan(cfg: dict, resources: list) -> list:
    """Решить, что продавать: [(rid, amount, rule)]. Чистая функция, без API.

    Режимы (exchange.rules):
      cap    — продавать, когда склад заполнен на threshold_pct % и больше;
      always — продавать всё сразу, как только количество >= min.
    keep — сколько единиц оставить на складе (0 = продавать всё, как кнопка в игре).
    """
    ex = cfg.get("exchange") or {}
    if not ex.get("enabled", True):
        return []
    from . import game_data
    by_rid = {}
    for r in resources or []:
        rid = r.get("id") or game_data.rid_by_name(r.get("name"))
        if rid:
            by_rid[rid] = r
    plan = []
    for rule in ex.get("rules") or []:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        rid = str(rule.get("rid") or "")
        if rid not in game_data.SELL_INFO or rid not in by_rid:
            continue
        r = by_rid[rid]
        count = r.get("current")
        if count is None:
            continue
        try:
            keep = max(0.0, float(rule.get("keep") or 0))
            min_amt = max(0.0, float(rule.get("min") or 0))
            threshold = float(rule.get("threshold_pct") or 90)
        except (TypeError, ValueError):
            continue
        mode = str(rule.get("mode") or "cap")
        if mode not in ("cap", "always"):
            continue
        if mode == "cap":
            pct = r.get("pct")
            if pct is None and r.get("max"):
                pct = count / r["max"] * 100
            if pct is None or pct < threshold:
                continue
        amount = round(count - keep, 3)
        if amount <= 0 or amount < min_amt:
            continue
        plan.append((rid, amount, rule))
    return plan


def _fmt_proceeds(rid: str, amount: float) -> str:
    """Строка выручки: байтовые ресурсы — как данные (ГБ), DDT — как DDT."""
    from . import game_data
    unit = game_data.sell_unit(rid)
    if not unit:
        return "?"
    price, is_ddt = unit
    total = amount * price
    if is_ddt:
        return "+" + f"{total:,.2f}".replace(",", " ").rstrip("0").rstrip(".") + " DDT"
    return "+" + game_data.fmt_bytes(total)


async def _auto_exchange(cfg: dict, resources: list, ctx: dict, timeout: int) -> list:
    """Продать ресурсы по правилам через sellItem. Обновляет resources на месте.

    Возвращает список отчётов [{rid, name, amount, proceeds, balance, left}],
    либо [] если обменивать нечего/обмен выключен. Ошибки — событие в журнал
    и уведомление, скан в целом не проваливается.
    """
    import time as _time
    from . import game_data
    plan = _exchange_plan(cfg, resources)
    if not plan:
        return []
    reports, errors = [], []
    for rid, amount, rule in plan:
        unit = game_data.sell_unit(rid)
        name = game_data.ru_name(rid, rid)
        outcome = tma.run_steps(tma.exchange_steps(rid, amount), dict(ctx), timeout=timeout)
        ok = outcome["ok"]
        res = outcome["ctx"]
        if ok and res.get("sell_left") is None:
            ok = False  # 200, но без result — считаем ошибкой
        if not ok:
            detail = "; ".join(f"{s['name']}: {s['detail']}" for s in outcome["results"] if s["status"] == "fail") \
                     or (res.get("sell_raw") or "")[:200]
            errors.append(f"{name} ×{amount:g}: {detail}")
            break  # auth мог протухнуть — остальные продажи тем же контекстом бессмысленны
        left = res.get("sell_left")
        # обновляем balances и состояние ресурса на месте
        if res.get("sell_coin") is not None:
            db.kv_set("balance_coin", res["sell_coin"])
        if res.get("sell_mcoin") is not None:
            db.kv_set("balance_mcoin", res["sell_mcoin"])
        for r in resources:
            if (r.get("id") or game_data.rid_by_name(r.get("name"))) == rid:
                r["current"] = left
                if r.get("max"):
                    r["pct"] = round(left / r["max"] * 100, 1)
                if (r.get("state") or "").startswith("склад переполнен"):
                    r["state"] = ""
                break
        balance = res.get("sell_mcoin") if unit[1] else res.get("sell_coin")
        balance_ru = "?"
        if balance is not None:
            balance_ru = f"{balance:g} DDT" if unit[1] else game_data.fmt_bytes(balance)
        reports.append({
            "rid": rid, "name": name, "amount": amount,
            "proceeds": _fmt_proceeds(rid, amount),
            "balance": balance,
            "balance_ru": balance_ru,
            "left": left,
        })
        db.event("exchange", f"Продано: {name}",
                 f"{name} ×{amount:g} → {reports[-1]['proceeds']} "
                 f"(остаток: {left:g}, режим: {'при заполнении' if rule.get('mode') == 'cap' else 'сразу'})")
        from . import timing as timing_mod
        _time.sleep(timing_mod.sell_pause_sec(cfg, f"{rid}:{amount:g}"))  # пауза без одинаковых значений
    if errors:
        db.event("exchange", "Автообмен: ошибка", "\n".join(errors)[:1000], severity="critical")
        notify_mod.notify_error(cfg, "Автообмен не удался", "\n".join(errors)[:300])
    if reports and cfg.get("notify", {}).get("events", {}).get("exchange_report", True):
        lines = [f"{x['name']} ×{x['amount']:g} → {x['proceeds']} (баланс: {x['balance_ru']})"
                 for x in reports]
        notify_mod.notify(cfg, "exchange_report", "Автообмен выполнен",
                          "\n".join(lines)[:400], open_game=False)
    return reports


def _store_balances(state: dict, ctx: dict) -> None:
    """Сохранить балансы валют из ответа скана (coin = данные, mCoin = DDT)."""
    coin = ctx.get("balance_coin")
    mcoin = ctx.get("balance_mcoin")
    gs = (state or {}).get("gameStats") or {}
    if coin is None:
        coin = gs.get("coin")
    if mcoin is None:
        mcoin = gs.get("mCoin")
    if coin is not None:
        db.kv_set("balance_coin", coin)
    if mcoin is not None:
        db.kv_set("balance_mcoin", mcoin)


def compute_ddt_eta(resources: list) -> dict:
    """Время до следующей единицы каждого DDT-продающегося ресурса.

    Модель по коду игры (progress — СЕКУНДЫ текущего крафта, 0..produceTime):
      1) реальная скорость craftPerMinuteReal уже учитывает нехватку входов
         (урана) — ETA = оставшаяся доля единицы / скорость;
      2) если реальная скорость 0 (добыча входа встала) — считаем по доходу
         входного ресурса: сколько его не хватает до остатка стоимости единицы;
      3) нет данных/воркеров — None (в панели «—»).
    Возвращает {rid: {eta_sec, at, stock, cap, workers, note}} для kv.
    """
    from . import game_data
    by_rid = {}
    for r in resources or []:
        rid = r.get("id") or game_data.rid_by_name(r.get("name"))
        if rid:
            by_rid[rid] = r
    out = {}
    now_iso = db.now_iso()
    for rid, info in game_data.SELL_INFO.items():
        if not info["is_ddt"]:
            continue
        row = by_rid.get(rid)
        if not row:
            continue
        ci = row.get("craft_info") or {}
        produce = game_data.produce_time(rid)
        item = {"at": now_iso, "stock": row.get("current"), "cap": row.get("max"),
                "workers": ci.get("workers"), "eta_sec": None, "note": ""}
        if not produce or not ci:
            item["note"] = "нет данных производства"
            out[rid] = item
            continue
        if not ci.get("workers"):
            item["note"] = "производство простаивает"
            out[rid] = item
            continue
        progress = min(max(float(ci.get("progress") or 0), 0.0), float(produce))
        remaining_share = 1.0 - progress / float(produce)
        craft_real = ci.get("craft_real")
        if craft_real:
            # реальная скорость (с учётом входов) → ETA текущей единицы
            item["eta_sec"] = round(remaining_share / float(craft_real) * 60.0)
        else:
            # стоп: не хватает входа — по его доходу (для урановых — уран)
            costs = game_data.craft_cost(rid)
            eta = None
            for in_rid, cnt in costs:
                in_row = by_rid.get(in_rid)
                if not in_row:
                    continue
                need = float(cnt) * remaining_share
                have = float(in_row.get("current") or 0)
                in_ci = in_row.get("craft_info") or {}
                in_rate = in_ci.get("craft_real") or 0  # единиц входа в минуту
                if in_rate and in_rate > 0:
                    cand = max(0.0, need - have) / float(in_rate) * 60.0
                    eta = cand if eta is None else max(eta, cand)
                elif have >= need:
                    cand = 0.0
                    eta = cand if eta is None else max(eta, cand)
            item["eta_sec"] = round(eta) if eta is not None else None
            if item["eta_sec"] is None:
                item["note"] = "нет дохода входных ресурсов"
        out[rid] = item
    return out


async def action_scan(cfg: dict) -> dict:
    """Скан: catch-up чтение чата бота + (если настроено) TMA API состояние ресурсов."""
    result = {"ok": True, "chat_alerts": [], "resources": [], "notes": []}
    tma_cfg = cfg.get("tma", {})
    chat_cfg = cfg.get("chat", {})
    client = await tgapi.connect(cfg)
    try:
        bot = await tgapi.get_bot(client, cfg)
        # 1) новые сообщения бота
        after_id = int(db.kv_get("last_chat_msg_id", 0) or 0)
        msgs, max_id = await tgapi.read_bot_messages(
            client, bot, limit=int(chat_cfg.get("read_last_messages", 20)), after_id=after_id
        )
        if max_id > after_id:
            db.kv_set("last_chat_msg_id", max_id)
        alerts = parse_chat_alerts(msgs, chat_cfg.get("parse_patterns") or [])
        for a in alerts:
            db.event("chat", a["title"], a["text"], severity=a["severity"])
            if a["notify"] and (cfg.get("notify", {}).get("events", {}).get("chat_alerts", True)):
                notify_mod.notify(cfg, "chat_alerts", a["title"], a["text"][:200],
                                  high=a["severity"] == "critical", open_game=True)
        result["chat_alerts"] = alerts

        # 2) TMA API скан состояния
        steps_scan = tma.get_steps(cfg, "scan")
        if tma_cfg.get("enabled", True) and steps_scan:
            webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
            ctx = tma.build_context(cfg, webview_url)
            outcome = tma.run_steps(steps_scan, ctx,
                                    timeout=int(tma_cfg.get("timeout_seconds", 35)))
            result["steps"] = outcome["results"]
            result["ok"] = outcome["ok"]
            if not outcome["ok"]:
                raise RuntimeError("Шаги скана TMA провалились: " + json.dumps(
                    [r for r in outcome["results"] if r["status"] == "fail"], ensure_ascii=False))
            # состояние Doomsday: initUser → gameStats.mines + passiveFarm
            state = outcome["ctx"].get("game_state")
            resources = parse_resources_doomsday(state) if isinstance(state, dict) else []
            if resources:
                _store_balances(state, outcome["ctx"])
                # ETA следующих DDT-ресурсов (для статусбара панели)
                db.kv_set("ddt_eta", compute_ddt_eta(resources))
                # автообмен ПЕРЕД порогами: обменянный склад не должен
                # порождать уведомление «почти полон»
                ex_timeout = int(tma_cfg.get("timeout_seconds", 35))
                ex_report = await _auto_exchange(cfg, resources, ctx, ex_timeout)
                if ex_report:
                    result["exchange"] = ex_report
                result["resources"] = resources
                db.resource_snapshot(resources)
                _check_thresholds(cfg, resources)
                # цикл производства: дедлайн и сигнал «требуется ребут»
                farm_ends_at = outcome["ctx"].get("farm_ends_at")
                if isinstance(farm_ends_at, (int, float)):
                    db.kv_set("passive_farm_ends_at", str(int(farm_ends_at)))
                    import time as _t
                    if farm_ends_at <= _t.time() * 1000:
                        _notify_reboot_needed(cfg)
                    else:
                        db.kv_set("notified_reboot_needed", [])
                result["farm_ends_at"] = farm_ends_at
            else:
                # универсальный путь: из последнего JSON-ответа или сырого текста
                last_json = None
                for var_val in outcome["ctx"].values():
                    if isinstance(var_val, (dict, list)):
                        last_json = var_val
                res_cfg = tma_cfg.get("resources") or {}
                if last_json is not None:
                    resources = parse_resources_from_json(last_json, res_cfg)
                if not resources:
                    raw = outcome["ctx"].get("state_raw") or ""
                    if raw:
                        try:
                            resources = parse_resources_from_json(json.loads(raw), res_cfg)
                        except ValueError:
                            resources = parse_resources_from_text(raw, res_cfg.get("text_patterns"))
                result["resources"] = resources
                if resources:
                    db.resource_snapshot(resources)
                    _check_thresholds(cfg, resources)
                else:
                    result["notes"].append(
                        "Ресурсы не распознаны: проверьте tma.resources (json_path/fields или text_patterns)"
                    )
                    db.event("scan", "Скан: ресурсы не распознаны",
                             "Ответ API получен, но паттерны ничего не нашли — настройте tma.resources",
                             severity="warn")
        else:
            result["notes"].append(
                "TMA-скан не настроен (tma.steps_scan пуст). Выполнялся только мониторинг чата бота."
            )
        db.kv_set("last_scan_ts", db.now_iso())
        db.event("scan", "Скан выполнен",
                 json.dumps({"chat_alerts": len(alerts), "resources": len(result["resources"])},
                            ensure_ascii=False))
        return result
    finally:
        await client.disconnect()


def _notify_reboot_needed(cfg: dict) -> None:
    """Однократное уведомление: цикл производства истёк, нужен ребут."""
    if db.kv_get("notified_reboot_needed"):
        return
    if cfg.get("notify", {}).get("events", {}).get("reboot_needed", True):
        notify_mod.notify(
            cfg, "reboot_needed", "Требуется ребут производства",
            "Цикл производства истёк — ресурсы не производятся. "
            "Бот сделает ребут автоматически в ближайший проход, "
            "или откройте игру и перезапустите цикл вручную.",
            high=True, open_game=True)
    db.kv_set("notified_reboot_needed", [db.now_iso()])


def _check_thresholds(cfg: dict, resources: list) -> None:
    """Пороговые уведомления о наполнении склада (с дедупликацией).

    Если задан notify.resource_filter (список id), уведомления приходят
    только по выбранным ресурсам; пустой список = все ресурсы.
    """
    warn_pct = float(cfg.get("notify", {}).get("warn_threshold_pct", 90) or 90)
    flt = set(cfg.get("notify", {}).get("resource_filter") or [])
    if flt:
        resources = [r for r in resources if _res_key(r) in flt]
    notified_full = set(db.kv_get("notified_full", []) or [])
    notified_warn = set(db.kv_get("notified_warn", []) or [])
    ev = cfg.get("notify", {}).get("events", {})
    now_full, now_warn = set(), set()
    for r in resources:
        pct = r.get("pct")
        name = r["name"]
        state = (r.get("state") or "").lower()
        is_full = (pct is not None and pct >= 100) or "переполн" in state or "full" in state
        is_warn = pct is not None and pct >= warn_pct
        if is_full:
            now_full.add(name)
            if name not in notified_full and ev.get("resource_full", True):
                notify_mod.notify(
                    cfg, "resource_full", f"Склад переполнен: {name}",
                    f"{name}: {_fmt(r['current'])} / {_fmt(r['max'])} — производство теряет "
                    f"эффективность. Зайдите в игру и разгрузите склад.",
                    high=True, open_game=True)
        elif is_warn:
            now_warn.add(name)
            if name not in notified_warn and name not in notified_full and ev.get("resource_warn", True):
                notify_mod.notify(
                    cfg, "resource_warn", f"Ресурс близок к максимуму: {name}",
                    f"{name}: {_fmt(r['current'])} / {_fmt(r['max'])} ({pct:.0f}%)",
                    open_game=True)
    db.kv_set("notified_full", sorted(now_full))
    db.kv_set("notified_warn", sorted(now_warn))


def _fmt(v):
    if v is None:
        return "?"
    if float(v).is_integer():
        return f"{int(v):,}".replace(",", " ")
    return f"{v:,.1f}".replace(",", " ")


async def action_reboot(cfg: dict) -> dict:
    """Ребут производства: TMA API rebootProduction (встроенный пресет Doomsday)."""
    result = {"ok": True, "steps": [], "notes": []}
    tma_cfg = cfg.get("tma", {})
    prev_cycle = db.kv_get("passive_farm_ends_at")  # цикл, который перезапускаем
    client = await tgapi.connect(cfg)
    try:
        bot = await tgapi.get_bot(client, cfg)
        if cfg.get("chat", {}).get("send_start_on_reboot", True):
            await tgapi.send_start(client, bot, "/start")
            result["steps"].append({"name": "chat:/start", "status": "ok", "detail": "Отправлен /start боту"})
        steps = tma.get_steps(cfg, "reboot")
        if tma_cfg.get("enabled", True) and steps:
            webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
            ctx = tma.build_context(cfg, webview_url)
            outcome = tma.run_steps(steps, ctx, timeout=int(tma_cfg.get("timeout_seconds", 35)))
            result["steps"] += outcome["results"]
            result["ok"] = outcome["ok"]
            ends_at = outcome["ctx"].get("reboot_ends_at")
            if outcome["ok"] and isinstance(ends_at, (int, float)):
                db.kv_set("passive_farm_ends_at", str(int(ends_at)))
                db.kv_set("notified_reboot_needed", [])
                result["farm_ends_at"] = ends_at
                result["cycle_until"] = _fmt_ms_deadline(ends_at)
        else:
            # TMA не настроен: «вход в игру» через webview + уведомление с кнопкой
            try:
                webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
                result["steps"].append({"name": "webview", "status": "ok",
                                        "detail": "Mini App игры открыт (сессия отмечена)"})
            except tgapi.TgError as e:
                result["steps"].append({"name": "webview", "status": "fail", "detail": str(e)})
            if cfg.get("notify", {}).get("events", {}).get("reboot_report", True):
                notify_mod.notify(
                    cfg, "reboot_report", "Время ребута производства",
                    "Автоматический ребут через API не настроен. Откройте игру и перезапустите "
                    "производство: вкладка «Производство» → текущий цикл → ребут.",
                    high=True, open_game=True)
            result["notes"].append("tma.steps_reboot не настроен — уведомление отправлено вручную")
        if result["ok"]:
            db.kv_set("last_reboot_ts", db.now_iso())
            db.kv_set("cycle_reboot_guard", prev_cycle)  # этот цикл уже перезапущен
            db.kv_set("notified_full", [])
            db.kv_set("notified_warn", [])
        body = json.dumps({"steps": result["steps"], "cycle_until": result.get("cycle_until")},
                          ensure_ascii=False)
        db.event("reboot", "Ребут производства" if result["ok"] else "Ребут: ошибка",
                 body, severity="info" if result["ok"] else "critical")
        if result["ok"] and cfg.get("notify", {}).get("events", {}).get("reboot_report", True):
            ok_steps = [s["name"] for s in result["steps"] if s["status"] == "ok"]
            extra = (f". Новый цикл до {result['cycle_until']}"
                     if result.get("cycle_until") else "")
            notify_mod.notify(cfg, "reboot_report", "Производство перезапущено",
                              "Шаги: " + (", ".join(ok_steps) if ok_steps else "нет данных") + extra,
                              open_game=False)
        elif not result["ok"]:
            failed = [f"{s['name']}: {s['detail']}" for s in result["steps"] if s["status"] == "fail"]
            notify_mod.notify_error(cfg, "Ребут не удался",
                                    "; ".join(failed)[:300] or "см. журнал")
        return result
    finally:
        await client.disconnect()


async def action_discover(cfg: dict) -> dict:
    """Discovery: открыть webview, скачать JS игры, найти эндпоинты API."""
    client = await tgapi.connect(cfg)
    try:
        bot = await tgapi.get_bot(client, cfg)
        # если нашли бота не по конфигу — исправляем настройку на будущее
        real = (getattr(bot, "username", None) or "").strip()
        configured = (cfg.get("telegram", {}).get("game_bot") or "").lstrip("@").strip()
        if real and real.lower() != configured.lower():
            cfg.setdefault("telegram", {})["game_bot"] = "@" + real
            cfgmod.save(cfg)
            db.event("config", "game_bot исправлен",
                     json.dumps({"was": configured or "—", "now": "@" + real},
                                ensure_ascii=False), severity="warning")
        # /start боту: создаёт/обновляет диалог и провоцирует кнопки запуска игры
        try:
            await tgapi.send_start(client, bot, "/start")
            await asyncio.sleep(2)
        except Exception as e:
            log.info("send_start(discover): %s", e)
        webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
        report = tma.discover_endpoints(
            webview_url, timeout=int(cfg.get("tma", {}).get("timeout_seconds", 25))
        )
        report["webview_url"] = webview_url
        report["game_bot"] = "@" + (real or "?")
        # найден работающий API (Firebase callable, проверен /syncTime) —
        # фиксируем base_url в конфиге, если он отличается от текущего
        api_base = report.get("api_base")
        if api_base and (cfg.get("tma", {}).get("base_url") or "").rstrip("/") != api_base:
            cfg.setdefault("tma", {})["base_url"] = api_base
            cfgmod.save(cfg)
            db.event("config", "tma.base_url обновлён",
                     json.dumps({"now": api_base}, ensure_ascii=False), severity="warning")
        db.kv_set("discovery", report)
        db.event("discover", "Discovery API игры",
                 json.dumps({"endpoints": report.get("endpoints", [])[:30],
                             "websockets": report.get("websockets", [])[:10],
                             "origin": report.get("origin")}, ensure_ascii=False))
        return report
    finally:
        await client.disconnect()


async def action_check(cfg: dict) -> dict:
    """Диагностика окружения. Возвращает список проверок [{name, ok, detail}]."""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        print(f"{'✔' if ok else '✘'} {name}: {detail}")

    add("Конфигурация валидна", not cfgmod.validate(cfg),
        "; ".join(cfgmod.validate(cfg)) or "OK")
    add("Файл сессии Telegram", os_path_exists_session(cfg),
        "есть" if os_path_exists_session(cfg) else "нет — выполните: doomsday login")
    add("termux-notification", notify_mod.termux_notification_available(),
        "доступен" if notify_mod.termux_notification_available()
        else "нет: pkg install termux-api + приложение Termux:API (F-Droid)")
    web_ok, web_detail = _panel_probe(cfg)
    add("Веб-панель", web_ok, web_detail)

    try:
        client = await tgapi.connect(cfg)
        try:
            bot = await tgapi.get_bot(client, cfg)
            add("Бот игры доступен", True, f"@{getattr(bot, 'username', cfg['telegram']['game_bot'])}")
            try:
                url = await tgapi.resolve_webview_url(client, bot, cfg)
                origin = tma.parse_webview_url(url)["origin"]
                add("Mini App (webview URL)", True, origin or url[:80])
                if tma.get_steps(cfg, "scan") or tma.get_steps(cfg, "reboot"):
                    add("TMA API шаги", True,
                        "встроенный пресет Doomsday (base_url: " + tma.get_base_url(cfg) + ")")
                else:
                    add("TMA API шаги", False, "не настроены — запустите doomsday discover")
            except tgapi.TgError as e:
                add("Mini App (webview URL)", False, str(e)[:200])
        finally:
            await client.disconnect()
    except tgapi.SessionNotAuthorized as e:
        add("Подключение Telegram", False, str(e))
    except tgapi.TgError as e:
        add("Подключение Telegram", False, str(e))
    except Exception as e:  # telethon импорт/сеть
        add("Подключение Telegram", False, f"{type(e).__name__}: {e}")

    ok_all = all(c["ok"] for c in checks)
    db.event("check", "Диагностика: " + ("всё в порядке" if ok_all else "есть замечания"),
             json.dumps(checks, ensure_ascii=False),
             severity="info" if ok_all else "warn")
    return {"ok": ok_all, "checks": checks}


def _panel_probe(cfg: dict):
    """Отвечает ли веб-панель на localhost (2 сек). (True, url) / (False, подсказка)."""
    import urllib.error
    import urllib.request
    from . import starter as starter_mod
    url = starter_mod.panel_url(cfg)
    try:
        urllib.request.urlopen(url + "/api/overview", timeout=2)
        return True, url
    except urllib.error.HTTPError:
        return True, url + " (PIN включён)"
    except (urllib.error.URLError, OSError, TimeoutError):
        return False, f"не отвечает на {url} — запустите: doomsday panel"


def os_path_exists_session(cfg: dict) -> bool:
    import os
    sp = tgapi.session_path(cfg) + ".session"
    return os.path.exists(sp)



async def action_summary(cfg: dict) -> dict:
    """Ежедневная сводка: ресурсы, счётчики, таймеры."""
    from . import starter as starter_mod
    resources = db.resources_latest_ru()
    today = db.now_iso()[:10]
    evs = db.events_list(limit=200)
    reboots = sum(1 for e in evs if e["kind"] == "reboot" and (e["ts"] or "").startswith(today))
    scans = sum(1 for e in evs if e["kind"] == "scan" and (e["ts"] or "").startswith(today))
    errors = sum(1 for e in evs if e["severity"] == "critical" and (e["ts"] or "").startswith(today))
    lines = []
    farm = starter_mod.farm_deadline_info(cfg)
    if farm.get("known"):
        if farm["active"]:
            h, m = farm["left_sec"] // 3600, (farm["left_sec"] % 3600) // 60
            lines.append(f"Цикл производства активен ещё {h} ч {m:02d} мин (до {farm['ends_at'][11:16]})")
        else:
            lines.append("Цикл производства ИСТЁК — нужен ребут")
    from . import game_data
    coin, mcoin = db.kv_get("balance_coin"), db.kv_get("balance_mcoin")
    if coin is not None:
        lines.append(f"Данные для серверов: {game_data.fmt_bytes(coin)}")
    if mcoin is not None:
        lines.append(f"DDT: {mcoin:g}")
    if resources:
        flt = set(cfg.get("notify", {}).get("resource_filter") or [])
        if flt:
            resources = [r for r in resources if _res_key(r) in flt]
        top = sorted(resources, key=lambda r: -(r.get("current") or 0))[:8]
        for r in top:
            mx = r["maximum"] if "maximum" in r else r.get("max")
            lines.append(f"{r['name']}: {_fmt(r.get('current'))} / {_fmt(mx)}")
    else:
        lines.append("данные о ресурсах пока не собраны")
    body = (f"Ребутов сегодня: {reboots}; сканов: {scans}; ошибок: {errors}.\n"
            "Ресурсы:\n" + "\n".join(lines))
    db.kv_set("last_summary_date", today)
    db.event("summary", "Сводка дня", body)
    if cfg.get("notify", {}).get("events", {}).get("daily_summary", True):
        notify_mod.notify(cfg, "daily_summary", "Сводка дня", body[:400])
    return {"ok": True, "body": body}


def run_coro(coro):
    return asyncio.run(coro)
