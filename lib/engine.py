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

def _exchange_plan(cfg: dict, resources: list, force_all: bool = False) -> list:
    """Решить, что продавать: [(rid, amount, rule)]. Чистая функция, без API.

    Режимы (exchange.rules):
      cap    — продавать, когда склад заполнен на threshold_pct % и больше;
      always — продавать всё сразу, как только количество >= min.
    keep — сколько единиц оставить на складе (0 = продавать всё, как кнопка в игре).

    force_all («собрать всю память», ручное действие из панели): план строится
    из ВСЕХ байтовых носителей (cassete/floppy/hdd) с amount = текущий склад
    и keep = 0 — мимо порогов, правил и переключателя exchange.enabled.
    DDT-ресурсы (uran_pills/u235/ddt_res) в форс-план НЕ входят: их продажа
    остаётся только на правилах — это премиум-валюта, «всю сразу» её не собирают.
    """
    from . import game_data
    by_rid = {}
    for r in resources or []:
        rid = r.get("id") or game_data.rid_by_name(r.get("name"))
        if rid:
            by_rid[rid] = r
    if force_all:
        plan = []
        for rid, info in game_data.SELL_INFO.items():
            if info.get("is_ddt"):
                continue
            count = (by_rid.get(rid) or {}).get("current")
            if count is None or count <= 0:
                continue
            plan.append((rid, round(count, 3),
                         {"rid": rid, "mode": "manual", "keep": 0, "enabled": True}))
        return plan
    ex = cfg.get("exchange") or {}
    if not ex.get("enabled", True):
        return []
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


def _fmt_gain(value: float, is_ddt: bool) -> str:
    """Фактическая прибавка валюты из ответа сервера (дельта баланса)."""
    from . import game_data
    if is_ddt:
        return "+" + f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".") + " DDT"
    return "+" + game_data.fmt_bytes(value)


def _sell_pause(cfg: dict, seed: str) -> None:
    """Пауза между вызовами sellItem (джиттер 0.4–1.6 с). Отдельная функция —
    чтобы тесты подменяли её и не ждали по-настоящему."""
    import time as _time
    from . import timing as timing_mod
    _time.sleep(timing_mod.sell_pause_sec(cfg, seed))


async def _auto_exchange(cfg: dict, resources: list, ctx: dict, timeout: int,
                           force_all: bool = False) -> list:
    """Продать ресурсы по правилам через sellItem. Обновляет resources на месте.

    force_all=True — «собрать всю память»: план из всех байтовых носителей
    (см. _exchange_plan), используется ручной кнопкой панели.

    Возвращает список отчётов [{rid, name, requested, amount, proceeds,
    proceeds_real, balance, balance_ru, left, attempts}], либо [] если
    обменивать нечего/обмен выключен. Ошибки — событие в журнал и
    уведомление, скан в целом не проваливается.

    Все цифры в отчётах — ФАКТИЧЕСКИЕ (диагностика 15.09 20:05: в сообщении
    «×N» стоял запланированный объём, а по факту со склада ушло заметно
    меньше; баланс из ответа при этом был верный). Поэтому:
      * amount   = сколько реально продано: count_before − resourceCount
                   из ответа сервера (НЕ запрошенное количество);
      * proceeds = дельта баланса валюты из ответа (fallback: amount × цена,
                   помечается как оценка);
      * частичная продажа добирается повторными sellItem (до 3 вызовов),
        пока остаток выше цели keep и продажи продолжают прогрессировать;
      * если сервер продал меньше запрошенного — в событие пишется сырой
        ответ, чтобы необычное поведение сервера было видно сразу.
    """
    from . import game_data

    plan = _exchange_plan(cfg, resources, force_all=force_all)
    if not plan:
        return []
    reports, errors = [], []
    # балансы ДО продаж — база для расчёта фактической выручки по дельте
    prev_coin = ctx.get("balance_coin")
    prev_mcoin = ctx.get("balance_mcoin")
    MAX_ATTEMPTS = 3
    for rid, amount, rule in plan:
        unit = game_data.sell_unit(rid)
        name = game_data.ru_name(rid, rid)
        try:
            keep = max(0.0, float(rule.get("keep") or 0))
        except (TypeError, ValueError):
            keep = 0.0
        row = None
        for r in resources:
            if (r.get("id") or game_data.rid_by_name(r.get("name"))) == rid:
                row = r
                break
        count_before = (row or {}).get("current")
        sold, left, attempts = 0.0, None, 0
        coin_now = mcoin_now = None
        raw_tail = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            asked = amount if attempt == 1 else round(left - keep, 3)
            if asked <= 0:
                break
            outcome = tma.run_steps(tma.exchange_steps(rid, asked), dict(ctx), timeout=timeout)
            ok = outcome["ok"]
            res = outcome["ctx"]
            if ok and res.get("sell_left") is None:
                ok = False  # 200, но без result — считаем ошибкой
            if not ok:
                detail = "; ".join(f"{s['name']}: {s['detail']}" for s in outcome["results"]
                                   if s["status"] == "fail") \
                         or (res.get("sell_raw") or "")[:200]
                errors.append(f"{name} ×{asked:g}: {detail}")
                break  # auth мог протухнуть — остальные продажи тем же контекстом бессмысленны
            prev_left, left = left, res.get("sell_left")
            coin_now, mcoin_now = res.get("sell_coin"), res.get("sell_mcoin")
            raw_tail = (res.get("sell_raw") or "")[-260:]
            base = count_before if attempt == 1 else prev_left
            step = (base - left) if (base is not None and left is not None) else None
            if step is None or step < 0:
                step = 0.0  # остаток вырос — сервер ничего не списал
            sold += step
            shortfall = (left - keep) if left is not None else 0.0
            if shortfall < 1.0 or step <= 0:
                break  # цель достигнута / дробный осадок / продажи не идут
            _sell_pause(cfg, f"{rid}:{attempt}")
        if left is None:
            continue  # продажа не состоялась — ошибка уже в errors

        # обновляем balances и состояние ресурса на месте
        if coin_now is not None:
            db.kv_set("balance_coin", coin_now)
        if mcoin_now is not None:
            db.kv_set("balance_mcoin", mcoin_now)
        if row is not None:
            row["current"] = left
            if row.get("max"):
                row["pct"] = round(left / row["max"] * 100, 1)
            if (row.get("state") or "").startswith("склад переполнен"):
                row["state"] = ""

        # фактическая выручка — по дельте баланса из ответа сервера
        is_ddt = bool(unit[1]) if unit else False
        if is_ddt:
            prev_bal, bal_now = prev_mcoin, mcoin_now
        else:
            prev_bal, bal_now = prev_coin, coin_now
        delta = (bal_now - prev_bal) if (bal_now is not None and prev_bal is not None) else None
        sold = round(sold, 3)
        if sold <= 0:
            proceeds, proceeds_real = "нет", False
        elif delta is not None and delta >= 0:
            proceeds, proceeds_real = _fmt_gain(delta, is_ddt), True
        else:
            proceeds, proceeds_real = _fmt_proceeds(rid, sold), False
        if bal_now is not None:
            prev_coin, prev_mcoin = coin_now, mcoin_now
        balance_ru = "?"
        if bal_now is not None:
            balance_ru = f"{bal_now:g} DDT" if is_ddt else game_data.fmt_bytes(bal_now)
        reports.append({
            "rid": rid, "name": name,
            "requested": amount,          # сколько собирались продать
            "amount": sold,               # сколько реально продано (по ответу)
            "proceeds": proceeds, "proceeds_real": proceeds_real,
            "balance": bal_now, "balance_ru": balance_ru,
            "left": left, "attempts": attempts,
        })
        partial = (left - keep) >= 1.0 or sold <= 0
        mode_ru = ("вручную (вся память)" if rule.get("mode") == "manual"
                   else "при заполнении" if rule.get("mode") == "cap" else "сразу")
        if sold > 0:
            head = f"продано {sold:g}"
            if partial:
                head += f" из {amount:g}"
            body = f"{head} → {proceeds} (остаток: {left:g}, режим: {mode_ru})"
            title = f"Продано: {name}"
        else:
            body = (f"сервер не списал ресурс (запрошено {amount:g}, остаток {left:g}, "
                    f"режим: {mode_ru})")
            title = f"Обмен не прошёл: {name}"
        if not proceeds_real and sold > 0:
            body += "  [выручка оценена по количеству]"
        if partial and raw_tail:
            body += f"\nответ сервера: {raw_tail}"
        db.event("exchange", title, body, severity="warn" if partial else "info")
        if not partial:
            _sell_pause(cfg, f"{rid}:done")
    if errors:
        db.event("exchange", "Автообмен: ошибка", "\n".join(errors)[:1000], severity="critical")
        notify_mod.notify_error(cfg, "Автообмен не удался", "\n".join(errors)[:300])
    if reports and (force_all or cfg.get("notify", {}).get("events", {})
                    .get("exchange_report", True)):
        lines = []
        for x in reports:
            ln = f"{x['name']} ×{x['amount']:g} → {x['proceeds']} (баланс: {x['balance_ru']})"
            req, amt = x.get("requested"), x.get("amount") or 0
            if req is not None and req - amt >= 1:
                ln += f" — продано меньше запрошенного ({req:g})"
            lines.append(ln)
        notify_mod.notify(cfg, "exchange_report",
                          "Память собрана (вручную)" if force_all else "Автообмен выполнен",
                          "\n".join(lines)[:600], open_game=False)
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


async def action_scan(cfg: dict, sell_all: bool = False) -> dict:
    """Скан: catch-up чтение чата бота + (если настроено) TMA API состояние ресурсов.

    sell_all=True — после скана продать ВСЮ память (байтовые носители)
    немедленно, мимо правил: так работает кнопка «Собрать всю память».
    """
    result = {"ok": True, "chat_alerts": [], "resources": [], "notes": []}
    if sell_all:
        result["sell_all"] = True
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
                ex_report = await _auto_exchange(cfg, resources, ctx, ex_timeout,
                                                 force_all=sell_all)
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
        db.kv_set("force_scan_at", None)  # контрольный скан после ребута выполнен
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


def _now_sec() -> float:
    import time as _t
    return _t.time()


def _is_session_error(results: list) -> bool:
    """Похоже ли падение шагов на отказ сессии игры.

    Сервер отвечает 403 PERMISSION_DENIED "Expired session" (внутренний статус
    418 — multisession guard), если hash сессии не зарегистрирован через
    initUser или его перехватило открытое на телефоне приложение.
    """
    for r in results or []:
        if (r.get("status") or "") != "fail":
            continue
        d = str(r.get("detail") or "").lower()
        if ("403" in d or "418" in d or "expired" in d or "permission" in d
                or "sessionexpired" in d or "unauthenticated" in d):
            return True
    return False


def reboot_fresh_cycle(ends_at_ms, now_sec: float, before_sec: int,
                       margin_sec: int = 60) -> bool:
    """Цикл производства уже «свежий» — ребут не нужен?

    True — до конца цикла больше, чем окно ребута + запас: значит производство
    уже кто-то перезапустил (например, вручную в игре), и вызывать
    rebootProduction нельзя — сбросим чужой свежий цикл. Нет данных о конце
    цикла — False (пусть решение принимает игра).
    """
    if not isinstance(ends_at_ms, (int, float)):
        return False
    return (ends_at_ms / 1000.0 - now_sec) > (before_sec + margin_sec)


def _force_scan_soon() -> None:
    """Назначить контрольный скан через несколько минут — не по интервалу.

    После ребута производство возобновляется и склады растут заново: обычный
    интервал скана (45 мин и больше с учётом Doze) слишком долог — панель и
    автообмен должны увидеть свежие данные почти сразу. Смещение стабильно
    по моменту ребута, ±2 минуты вокруг 7 минут.
    """
    import datetime as _dt
    from . import timing as timing_mod
    delay = max(60.0, 7 * 60 + timing_mod.jitter_offset(f"force-scan-{db.now_iso()}", 120))
    at = _dt.datetime.now() + _dt.timedelta(seconds=delay)
    db.kv_set("force_scan_at", at.isoformat(timespec="seconds"))


async def _refresh_after_reboot(cfg: dict, ctx: dict, timeout: int,
                                init_step: dict, rebooted: bool) -> list:
    """Обновить ресурсы панели сразу после ребута, не дожидаясь скана.

    Диагностика 15.09: ребут в 19:15 прошёл, но следующий скан по расписанию
    был только через 45+ минут (и позже из-за Doze) — всё это время панель
    показывала снимок с «требуется ребут» на каждом ресурсе, а кассета,
    остановившаяся в 20 единицах от порога, так и не обменялась.

    После успешного ребута повторяем initUser той же сессией: он возвращает
    точный новый цикл и актуальные состояния производства. Если повтор не
    удался — берём состояние из первого initUser и правим статус «требуется
    ребут» вручную (производство уже идёт). При пропуске ребута («цикл уже
    свежий») состояние из первого initUser и так актуально.

    Затем: снимок ресурсов в панель, автообмен по правилам (за время простоя
    склад мог дойти до порога) и контрольный скан через несколько минут.
    Возвращает отчёт обмена (может быть пустым).
    """
    state = ctx.get("game_state") if isinstance(ctx.get("game_state"), dict) else None
    ex_ctx = ctx
    if rebooted and state is not None:
        re_out = tma.run_steps([init_step], dict(ctx), timeout=timeout)
        if re_out["ok"] and isinstance(re_out["ctx"].get("game_state"), dict):
            state = re_out["ctx"]["game_state"]
            ex_ctx = re_out["ctx"]
            _store_balances(state, ex_ctx)
            ends = ex_ctx.get("farm_ends_at")
            if isinstance(ends, (int, float)):
                db.kv_set("passive_farm_ends_at", str(int(ends)))
    resources = parse_resources_doomsday(state) if isinstance(state, dict) else []
    if not resources:
        _force_scan_soon()
        return []
    if rebooted:
        # данные могли быть взяты до ребута: производство уже возобновилось
        for r in resources:
            if r.get("state") == "требуется ребут":
                r["state"] = ""
    db.kv_set("ddt_eta", compute_ddt_eta(resources))
    ex_report = await _auto_exchange(cfg, resources, ex_ctx, timeout)
    # снимок ПОСЛЕ обмена: панель видит постпродажные остатки
    db.resource_snapshot(resources)
    _force_scan_soon()
    return ex_report


async def action_reboot(cfg: dict) -> dict:
    """Ребут производства: TMA API rebootProduction (встроенный пресет Doomsday).

    Протокол сессий игры (диагностика 2026-09-15): сервер принимает только
    hash, зарегистрированный через initUser — первый вызов веб-приложения
    при загрузке. Поэтому пресет ребута сначала вызывает initUser (регистрирует
    сессию и заодно возвращает актуальный passiveFarm.endsAt) и только затем
    rebootProduction той же сессией. Если initUser покажет, что цикл уже
    свежий — ребут пропускается. При отказе сессии (игра открыта на телефоне
    в этот момент) — один повтор с новым webview.
    """
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
            custom = bool((cfg.get("tma") or {}).get("steps_reboot"))
            before_sec = int(cfg.get("schedules", {}).get("reboot_before_end_minutes", 10) or 10) * 60
            init_step = next((s for s in steps if s.get("name") == "initUser"), None)
            reboot_step = next((s for s in steps if s.get("name") == "rebootProduction"), None)
            preset_path = (not custom) and init_step is not None and reboot_step is not None

            async def _tma_pass() -> dict:
                webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
                ctx = tma.build_context(cfg, webview_url)
                timeout = int(tma_cfg.get("timeout_seconds", 35))
                if not preset_path:
                    # кастомные шаги — как раньше, одним конвейером
                    return tma.run_steps(steps, ctx, timeout=timeout)
                # пресет: initUser (регистрация сессии) → rebootProduction
                outcome = tma.run_steps([init_step], ctx, timeout=timeout)
                if not outcome["ok"]:
                    return outcome
                state = outcome["ctx"].get("game_state")
                if isinstance(state, dict):
                    _store_balances(state, outcome["ctx"])  # свежие балансы в панель
                ends = outcome["ctx"].get("farm_ends_at")
                if reboot_fresh_cycle(ends, _now_sec(), before_sec):
                    outcome["results"].append({
                        "name": "rebootProduction", "status": "skip",
                        "detail": f"цикл уже свежий (до {_fmt_ms_deadline(ends)}) — ребут не нужен"})
                    db.kv_set("passive_farm_ends_at", str(int(ends)))
                    db.kv_set("notified_reboot_needed", [])
                    result["farm_ends_at"] = ends
                    result["cycle_until"] = _fmt_ms_deadline(ends)
                    return outcome
                out = tma.run_steps([reboot_step], ctx, timeout=timeout)
                outcome["results"] += out["results"]
                outcome["ok"] = out["ok"]
                outcome["ctx"] = out["ctx"]
                return outcome

            outcome = await _tma_pass()
            if not outcome["ok"] and _is_session_error(outcome["results"]):
                # сессию перехватило приложение, открытое на телефоне прямо сейчас:
                # получаем новый webview (новая сессия) и пробуем ещё раз;
                # обе попытки остаются в журнале шагов
                result["steps"] += outcome["results"]
                result["notes"].append("попытка отклонена (сессия) — повтор с новым webview")
                await asyncio.sleep(3)
                try:
                    outcome = await _tma_pass()
                except tgapi.TgError as e:
                    result["steps"].append({"name": "webview", "status": "fail", "detail": str(e)})
                    outcome = {"ok": False, "results": [], "ctx": {}}
            result["steps"] += outcome["results"]
            result["ok"] = outcome["ok"]
            ends_at = outcome["ctx"].get("reboot_ends_at")
            if outcome["ok"] and isinstance(ends_at, (int, float)):
                db.kv_set("passive_farm_ends_at", str(int(ends_at)))
                db.kv_set("notified_reboot_needed", [])
                result["farm_ends_at"] = ends_at
                result["cycle_until"] = _fmt_ms_deadline(ends_at)
            if outcome["ok"] and preset_path:
                # панель сразу видит новый цикл: ресурсы/балансы/автообмен
                # без ожидания ближайшего скана по интервалу
                skipped = any(s.get("status") == "skip" for s in outcome["results"])
                try:
                    ex_report = await _refresh_after_reboot(
                        cfg, outcome["ctx"], int(tma_cfg.get("timeout_seconds", 35)),
                        init_step, rebooted=not skipped)
                    if ex_report:
                        result["exchange"] = ex_report
                except Exception as e:  # обновление не должно ронять ребут
                    result["notes"].append(f"обновление панели после ребута: {e}")
        else:
            # TMA не настроен: «вход в игру» через webview + уведомление с кнопкой
            try:
                await tgapi.resolve_webview_url(client, bot, cfg)
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
        any_skip = any(s.get("status") == "skip" for s in result["steps"])
        title = ("Ребут: цикл уже свежий" if result["ok"] and any_skip
                 else "Ребут производства" if result["ok"] else "Ребут: ошибка")
        db.event("reboot", title, body, severity="info" if result["ok"] else "critical")
        if result["ok"] and cfg.get("notify", {}).get("events", {}).get("reboot_report", True):
            ok_steps = [s["name"] for s in result["steps"] if s["status"] == "ok"]
            skipped = [s for s in result["steps"] if s["status"] == "skip"]
            extra = (f". Новый цикл до {result['cycle_until']}"
                     if result.get("cycle_until") else "")
            if skipped:
                notify_mod.notify(cfg, "reboot_report", "Ребут не понадобился",
                                  skipped[0]["detail"] + extra, open_game=False)
            else:
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
