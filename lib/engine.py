# -*- coding: utf-8 -*-
"""Высокоуровневые действия: скан ресурсов, ребут производства, сводка, диагностика."""
import asyncio
import json
import logging
import os
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


def _mk_resource(name, cur, mx, state=""):
    pct = None
    if cur is not None and mx is not None and mx > 0:
        pct = round(cur / mx * 100, 1)
    return {"name": name[:64], "current": cur, "max": mx, "pct": pct, "state": state or ""}


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


# ---------------- действия ----------------

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
        if tma_cfg.get("enabled", True) and (tma_cfg.get("steps_scan") or []):
            webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
            ctx = tma.build_context(cfg, webview_url)
            outcome = tma.run_steps(tma_cfg.get("steps_scan"), ctx,
                                    timeout=int(tma_cfg.get("timeout_seconds", 25)))
            result["steps"] = outcome["results"]
            result["ok"] = outcome["ok"]
            if not outcome["ok"]:
                raise RuntimeError("Шаги скана TMA провалились: " + json.dumps(
                    [r for r in outcome["results"] if r["status"] == "fail"], ensure_ascii=False))
            # ресурсы: из последнего ответа JSON или из сырого текста
            resources = []
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


def _check_thresholds(cfg: dict, resources: list) -> None:
    """Пороговые уведомления о наполнении склада (с дедупликацией)."""
    warn_pct = float(cfg.get("notify", {}).get("warn_threshold_pct", 90) or 90)
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
    """Ребут производства: /start боту + TMA API шаги (если настроены)."""
    result = {"ok": True, "steps": [], "notes": []}
    tma_cfg = cfg.get("tma", {})
    client = await tgapi.connect(cfg)
    try:
        bot = await tgapi.get_bot(client, cfg)
        if cfg.get("chat", {}).get("send_start_on_reboot", True):
            await tgapi.send_start(client, bot, "/start")
            result["steps"].append({"name": "chat:/start", "status": "ok", "detail": "Отправлен /start боту"})
        steps = tma_cfg.get("steps_reboot") or []
        if tma_cfg.get("enabled", True) and steps:
            webview_url = await tgapi.resolve_webview_url(client, bot, cfg)
            ctx = tma.build_context(cfg, webview_url)
            outcome = tma.run_steps(steps, ctx, timeout=int(tma_cfg.get("timeout_seconds", 25)))
            result["steps"] += outcome["results"]
            result["ok"] = outcome["ok"]
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
            db.kv_set("notified_full", [])
            db.kv_set("notified_warn", [])
        body = json.dumps(result["steps"], ensure_ascii=False)
        db.event("reboot", "Ребут производства" if result["ok"] else "Ребут: ошибка",
                 body, severity="info" if result["ok"] else "critical")
        if result["ok"] and cfg.get("notify", {}).get("events", {}).get("reboot_report", True):
            ok_steps = [s["name"] for s in result["steps"] if s["status"] == "ok"]
            notify_mod.notify(cfg, "reboot_report", "Производство перезапущено",
                              "Шаги: " + (", ".join(ok_steps) if ok_steps else "нет данных"),
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
    from . import paths as paths_mod
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
    dl = paths_mod.resolve_download_dir(cfg.get("updater", {}).get("download_dir", ""))
    add("Каталог Загрузок", os.path.isdir(dl), dl)

    try:
        client = await tgapi.connect(cfg)
        try:
            bot = await tgapi.get_bot(client, cfg)
            add("Бот игры доступен", True, f"@{getattr(bot, 'username', cfg['telegram']['game_bot'])}")
            try:
                url = await tgapi.resolve_webview_url(client, bot, cfg)
                origin = tma.parse_webview_url(url)["origin"]
                add("Mini App (webview URL)", True, origin or url[:80])
                if (cfg.get("tma", {}).get("steps_scan") or cfg.get("tma", {}).get("steps_reboot")):
                    add("TMA API шаги", True, "настроены")
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


def os_path_exists_session(cfg: dict) -> bool:
    import os
    sp = tgapi.session_path(cfg) + ".session"
    return os.path.exists(sp)


async def action_summary(cfg: dict) -> dict:
    """Ежедневная сводка: ресурсы, счётчики, таймеры."""
    resources = db.resources_latest()
    today = db.now_iso()[:10]
    evs = db.events_list(limit=200)
    reboots = sum(1 for e in evs if e["kind"] == "reboot" and (e["ts"] or "").startswith(today))
    scans = sum(1 for e in evs if e["kind"] == "scan" and (e["ts"] or "").startswith(today))
    errors = sum(1 for e in evs if e["severity"] == "critical" and (e["ts"] or "").startswith(today))
    lines = []
    if resources:
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
