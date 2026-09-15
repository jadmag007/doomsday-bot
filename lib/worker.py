# -*- coding: utf-8 -*-
"""CLI-ядро: cron-циклы и все команды doomsday.

Запускается через bin/doomsday. Поддерживает:
  cron                  — плановый проход (вызывается cronie каждые 10 мин)
  scan|reboot|summary|discover|check|test-notify|login|setup|status|log|web|selftest
  panel                 — открыть веб-панель в браузере (при необходимости — запустить)
  start|stop            — стартёр: git-обновление + все службы / полная остановка
  logs-push             — выгрузить логи и отчёты в ветку logs репозитория
  git-auth              — сохранить GitHub PAT для git pull/push без запроса пароля
"""
import argparse
import datetime
import fcntl
import json
import logging
import logging.handlers
import os
import re
import sys

from . import config as cfgmod
from . import db
from . import notify as notify_mod
from . import paths
from . import engine

log = logging.getLogger("doomsday")


# ---------------- инфраструктура ----------------

def setup_logging(verbose: bool = False) -> None:
    paths.ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    try:
        fh = logging.handlers.RotatingFileHandler(
            paths.LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


class SingleInstance:
    """Блокировка воркера: cron — неблокирующая, ручные команды — с ожиданием."""

    def __init__(self, wait: bool):
        self.wait = wait
        self.fd = None

    def __enter__(self):
        self.fd = open(paths.LOCK_PATH, "w")
        flags = fcntl.LOCK_EX
        if not self.wait:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.fd, flags)
            self.fd.write(str(os.getpid()))
            self.fd.flush()
            return self
        except BlockingIOError:
            print("Другой проход воркера уже выполняется — выходим.")
            raise SystemExit(0)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.fd.close()
        except OSError:
            pass
        return False


def _ts(key):
    v = db.kv_get(key)
    try:
        return datetime.datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def _due(now, cfg: dict, name: str) -> bool:
    """Настало ли время действия: интервал истёк и не слишком рано после ошибки."""
    sch = cfg.get("schedules", {})
    if name == "reboot":
        interval = float(sch.get("reboot_interval_hours", 12) or 12) * 3600
        last_ok = _ts("last_reboot_ts")
    elif name == "scan":
        interval = int(sch.get("scan_interval_minutes", 60) or 60) * 60
        last_ok = _ts("last_scan_ts")
    else:
        return True
    if last_ok is not None and (now - last_ok).total_seconds() < interval:
        return False
    # троттлинг повторов при ошибках
    retry = int(sch.get("retry_failed_minutes", 20) or 20) * 60
    last_fail = _ts(f"last_fail_{name}")
    if last_fail is not None and (now - last_fail).total_seconds() < retry:
        return False
    return True


def _reboot_due_cycle(cfg: dict, now) -> bool:
    """Пора ли ребутить производство — по игровому циклу.

    Основной режим: reboot_before_end_minutes до конца цикла игры
    (passive_farm_ends_at из скана/ребута) — фарм не прерывается.
    Если цикл уже истёк — ребут немедленно (окно пропущено).
    Если данных о цикле нет (скан не давал passiveFarm) — запасная
    логика по интервалу от последнего ребута (режим без TMA).
    """
    sch = cfg.get("schedules", {})
    ends_raw = db.kv_get("passive_farm_ends_at")
    try:
        ends_sec = float(ends_raw) / 1000.0 if ends_raw else None
    except (TypeError, ValueError):
        ends_sec = None
    if ends_sec is None:
        return _due(now, cfg, "reboot")
    # этот цикл уже перезапускали, а новый дедлайн не получен — час не дёргаемся
    guard = db.kv_get("cycle_reboot_guard")
    try:
        guard_sec = float(guard) / 1000.0 if guard else None
    except (TypeError, ValueError):
        guard_sec = None
    if guard_sec is not None and abs(guard_sec - ends_sec) < 1 \
            and now.timestamp() < ends_sec + 3600:
        return False
    before_min = int(sch.get("reboot_before_end_minutes", 10) or 0)
    if now.timestamp() < ends_sec - before_min * 60:
        return False  # окно ребута ещё не открылось
    # окно открыто (или цикл истёк) — но не чаще, чем разрешает троттлинг ошибок
    retry = int(sch.get("retry_failed_minutes", 20) or 20) * 60
    last_fail = _ts("last_fail_reboot")
    if last_fail is not None and (now - last_fail).total_seconds() < retry:
        return False
    return True


def _fmt_delta(sec) -> str:
    if sec is None:
        return "нет данных"
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return (f"{h}ч {m:02d}м" if h else f"{m} мин") + (" назад" if sec >= 0 else "")


# ---------------- cron-планировщик ----------------

def cmd_cron(args) -> int:
    cfg = cfgmod.load()
    now = datetime.datetime.now()

    actions = []
    # 1) ребут производства — по игровому циклу (за N минут до конца)
    if _reboot_due_cycle(cfg, now):
        actions.append("reboot")
    # 2) скан ресурсов
    if _due(now, cfg, "scan"):
        actions.append("scan")
    # 3) сводка дня
    st = str(cfg.get("schedules", {}).get("summary_time") or "20:00")
    m = re.match(r"^(\d{1,2}):(\d{2})$", st)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= due and db.kv_get("last_summary_date") != now.date().isoformat():
            actions.append("summary")

    print(f"[cron] {db.now_iso()} план: {actions or 'ничего не подошло'}")
    for name in actions:
        try:
            r = _run_action(cfg, name)
            if r.get("ok", True):
                db.kv_set(f"last_fail_{name}", None)
            else:
                db.kv_set(f"last_fail_{name}", db.now_iso())
        except Exception as e:
            db.kv_set(f"last_fail_{name}", db.now_iso())
            log.exception("Действие %s провалилось: %s", name, e)
            notify_mod.notify_error(cfg, f"Ошибка: {name}", str(e)[:300])
            db.kv_set("last_error_ts", db.now_iso())
    return 0


def _run_action(cfg, name: str) -> dict:
    if name == "reboot":
        return engine.run_coro(engine.action_reboot(cfg))
    if name == "scan":
        return engine.run_coro(engine.action_scan(cfg))
    if name == "summary":
        return engine.run_coro(engine.action_summary(cfg))
    raise ValueError(f"Неизвестное действие: {name}")


# ---------------- отдельные команды ----------------

def cmd_scan(args) -> int:
    cfg = cfgmod.load()
    r = engine.run_coro(engine.action_scan(cfg))
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("ok") else 1


def cmd_exchange(args) -> int:
    """Скан + автообмен сейчас (не ждём интервала): продать по правилам."""
    cfg = cfgmod.load()
    r = engine.run_coro(engine.action_scan(cfg))
    ex = r.get("exchange") or []
    if ex:
        print("Продано:")
        for x in ex:
            print(f"  - {x['name']} ×{x['amount']:g} → {x['proceeds']} "
                  f"(остаток: {x['left']:g}, баланс: {x['balance_ru']})")
    else:
        print("По правилам обмена продавать нечего.")
    print(json.dumps({"ok": r.get("ok"), "resources": len(r.get("resources") or [])},
                     ensure_ascii=False))
    return 0 if r.get("ok") else 1


def cmd_reboot(args) -> int:
    cfg = cfgmod.load()
    r = engine.run_coro(engine.action_reboot(cfg))
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("ok") else 1


def cmd_summary(args) -> int:
    cfg = cfgmod.load()
    r = engine.run_coro(engine.action_summary(cfg))
    print(r.get("body", ""))
    return 0


def cmd_discover(args) -> int:
    cfg = cfgmod.load()
    try:
        r = engine.run_coro(engine.action_discover(cfg))
    except Exception as e:
        # вместо traceback — диагностический отчёт для разбора
        import traceback
        os.makedirs(paths.REPORTS_DIR, exist_ok=True)
        report = {
            "error": f"{type(e).__name__}: {e}",
            "traceback_tail": traceback.format_exc()[-4000:],
            "game_bot": cfg.get("telegram", {}).get("game_bot"),
            "app_short_name": cfg.get("telegram", {}).get("app_short_name"),
            "time": db.now_iso(),
        }
        try:
            with open(paths.VERSION_PATH, encoding="utf-8") as f:
                report["version"] = f.read().strip()
        except OSError:
            report["version"] = "?"
        out = os.path.join(paths.REPORTS_DIR,
                           f"discovery-error-{db.now_iso().replace(':', '')}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"✘ Discovery не удался: {type(e).__name__}: {e}")
        print(f"  Диагностика сохранена: {out}")
        print("  Передать ассистенту: doomsday logs-push")
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    out = os.path.join(paths.REPORTS_DIR, f"discovery-{db.now_iso().replace(':', '')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    print(f"\nОтчёт сохранён: {out}")
    print("Передать ассистенту: doomsday logs-push (выгрузит в ветку logs репозитория)")
    return 0


def cmd_check(args) -> int:
    cfg = cfgmod.load()
    r = engine.run_coro(engine.action_check(cfg))
    return 0 if r.get("ok") else 1


def cmd_test_notify(args) -> int:
    cfg = cfgmod.load()
    ok = notify_mod.notify(cfg, "errors", "Тест уведомлений",
                           "Если вы это видите — пуш-уведомления работают!",
                           high=False, open_game=True)
    print("Push доставлен" if ok else "Push НЕ доставлен — см. журнал (termux-api и Termux:API)")
    return 0 if ok else 1


def cmd_login(args) -> int:
    cfg = cfgmod.load()

    async def _login():
        client = await engine.tgapi.connect(cfg, interactive=True)
        me = await client.get_me()
        await client.disconnect()
        return me

    me = engine.run_coro(_login())
    print(f"Вход выполнен: {getattr(me, 'first_name', '')} (@{getattr(me, 'username', '')})")
    db.event("login", "Вход в Telegram", f"@{getattr(me, 'username', '')}")
    return 0


def cmd_setup(args) -> int:
    """Интерактивный мастер первого запуска."""
    cfg = cfgmod.load()
    print("=== Настройка Doomsday Tyranny Bot ===")
    print("(Enter — оставить текущее значение в [скобках])\n")

    def ask(prompt, cur, default=""):
        shown = cur if cur not in ("", None) else default
        v = input(f"{prompt} [{shown}]: ").strip()
        return v or (shown if shown != "" else default)

    tg = cfg["telegram"]
    api_id = ask("api_id с my.telegram.org", tg.get("api_id", ""))
    if api_id.isdigit():
        tg["api_id"] = int(api_id)
    api_hash = ask("api_hash с my.telegram.org", "••••" if cfgmod.api_hash_stored() else "")
    if api_hash and "•" not in api_hash:
        tg["api_hash"] = api_hash
    tg["phone"] = ask("Телефон (для входа, напр. +79991234567)", tg.get("phone", ""))
    tg["game_bot"] = "@" + ask("Бот игры", (tg.get("game_bot", "@DoomsDayTyrannybot") or "@DoomsDayTyrannybot").lstrip("@")).lstrip("@")

    sch = cfg["schedules"]
    rbm = ask("Ребут за N минут до конца цикла производства", sch.get("reboot_before_end_minutes", 10))
    try:
        sch["reboot_before_end_minutes"] = max(0, min(360, int(rbm)))
    except ValueError:
        pass
    scm = ask("Интервал скана ресурсов, минут", sch.get("scan_interval_minutes", 60))
    try:
        sch["scan_interval_minutes"] = max(4, min(1440, int(scm)))
    except ValueError:
        pass
    sch["summary_time"] = ask("Время daily-сводки ЧЧ:ММ", sch.get("summary_time", "20:00"))

    cfgmod.save(cfg)
    print("\nКонфигурация сохранена:", paths.CONFIG_PATH)
    errs = cfgmod.validate(cfg)
    if errs:
        print("ВНИМАНИЕ:", "; ".join(errs))
    print("\nДальше: doomsday login  →  doomsday check  →  doomsday discover")
    return 0


def cmd_status(args) -> int:
    cfg = cfgmod.load()
    from . import starter as starter_mod
    now = datetime.datetime.now()
    ver = paths.read_version()
    last_reboot, last_scan = _ts("last_reboot_ts"), _ts("last_scan_ts")
    minutes = int(cfg["schedules"].get("scan_interval_minutes", 60))
    ns = (last_scan or now) + datetime.timedelta(minutes=minutes)
    before_min = int(cfg["schedules"].get("reboot_before_end_minutes", 10))
    print(f"Doomsday Tyranny Bot v{ver}")
    print(f"Бот игры:            {cfg['telegram']['game_bot']}")
    print(f"Последний ребут:     {_fmt_delta((now - last_reboot).total_seconds()) if last_reboot else 'нет данных'}")
    print(f"Последний скан:      {_fmt_delta((now - last_scan).total_seconds()) if last_scan else 'нет данных'}"
          f"  → следующий ~{ns.strftime('%d.%m %H:%M') if last_scan else '?'}")
    farm = starter_mod.farm_deadline_info(cfg)
    if farm.get("known"):
        if farm["active"]:
            print(f"Цикл производства:   активен, осталось {_fmt_delta(farm['left_sec'])} (до {farm['ends_at'][11:16]})")
            if farm.get("reboot_at"):
                print(f"Авто-ребут:          ~{farm['reboot_at'][11:16]} "
                      f"(за {before_min} мин до конца цикла)")
        else:
            print(f"Цикл производства:   ИСТЁК {_fmt_delta(-farm['left_sec'])} — ребут в ближайший проход")
    else:
        print("Цикл производства:   нет данных (появится после первого скана/ребута)")
    res = db.resources_latest_ru()
    if res:
        print("Ресурсы (последний скан):")
        for r in res[:12]:
            mx = r.get("maximum")
            pct = f" ({r['current'] / mx * 100:.0f}%)" if mx else ""
            print(f"  - {r['name']}: {r['current']} / {mx if mx is not None else '?'}{pct} {r.get('state') or ''}")
    from . import game_data
    coin, mcoin = db.kv_get("balance_coin"), db.kv_get("balance_mcoin")
    if coin is not None:
        print(f"Данные для серверов:  {game_data.fmt_bytes(coin)}")
    if mcoin is not None:
        print(f"DDT (премиум):        {mcoin:g}")
    ex = cfg.get("exchange") or {}
    if ex.get("enabled", True) and (ex.get("rules") or []):
        on = [str(r.get("rid")) for r in ex["rules"] if r.get("enabled", True)]
        print(f"Автообмен:            вкл ({', '.join(on) if on else 'правил нет'})")
    url = starter_mod.panel_url(cfg)
    print(f"Веб-панель:          {url}")
    print("Открыть панель:      doomsday panel")
    return 0


def cmd_log(args) -> int:
    try:
        with open(paths.LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        print("Лог пуст")
        return 0
    n = int(getattr(args, "lines", 50) or 50)
    for line in lines[-n:]:
        print(line.rstrip())
    return 0


def cmd_start(args) -> int:
    from . import starter
    return starter.cmd_start(args)


def cmd_stop(args) -> int:
    from . import starter
    return starter.cmd_stop(args)


def cmd_logs_push(args) -> int:
    from . import logspush
    return logspush.cmd_logs_push(args)


def cmd_git_auth(args) -> int:
    from . import logspush
    return logspush.cmd_git_auth(args)


def cmd_web(args) -> int:
    from . import webui
    cfg = cfgmod.load()
    webui.serve_forever(cfg)
    return 0


def cmd_panel(args) -> int:
    from . import starter
    return starter.cmd_panel(args)


def cmd_web_restart(args) -> int:
    from . import starter
    return starter.cmd_web_restart(args)


# ---------------- selftest ----------------

def cmd_selftest(args) -> int:
    """Быстрые юнит-тесты без Telegram: конфиг, БД, версии, парсеры, TMA-драйвер."""
    import tempfile
    from . import tma as tma_mod

    failures = []

    def check(name, fn):
        try:
            ok, detail = fn()
            status = "ok" if ok else "FAIL"
            print(f"  {status}  {name}" + (f": {detail}" if detail else ""))
            if not ok:
                failures.append(name)
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failures.append(name)

    print("selftest: конфигурация")
    check("дефолты сливаются", lambda: (bool(cfgmod.DEFAULTS["schedules"]["reboot_interval_hours"] == 12), ""))
    check("миграция добавляет недостающие ключи",
          lambda: (cfgmod._deep_merge(cfgmod.DEFAULTS, {"web": {"port": 9999}})["web"]["pin"] == "" and
                    cfgmod._deep_merge(cfgmod.DEFAULTS, {"web": {"port": 9999}})["web"]["port"] == 9999, ""))
    check("валидация ловит пустой api_id",
          lambda: (len(cfgmod.validate({"telegram": {}})) > 0, ""))
    check("маскирование api_hash",
          lambda: ("•" in cfgmod.masked({"telegram": {"api_hash": "abcdef123456"}})["telegram"]["api_hash"], ""))

    print("selftest: парсеры")
    text = "Дерево: 1 200 / 5 000\nМеталл: 4 999/10 000\nУран: 9 800 / 10 000\nСклад переполнен: Нефть"
    res = engine.parse_resources_from_text(text, cfgmod.DEFAULTS["tma"]["resources"]["text_patterns"])
    check("regex-парсер ресурсов", lambda: (len(res) >= 3 and any(r["name"].startswith("Дерево") for r in res),
                                            f"найдено {len(res)}"))
    check("пороговые проценты", lambda: (any(r["pct"] and r["pct"] >= 90 for r in res), ""))
    alerts = engine.parse_chat_alerts(
        [{"id": 1, "date": "x", "text": "Внимание: Склад переполнен!"}],
        cfgmod.DEFAULTS["chat"]["parse_patterns"])
    check("парсер уведомлений чата", lambda: (len(alerts) == 1, alerts[0]["title"] if alerts else ""))

    print("selftest: TMA-драйвер")
    parsed = tma_mod.parse_webview_url(
        "https://game.example.com/app/?tgWebAuthData=user%3D1%26auth_date%3D1&tgWebVersion=8")
    check("разбор webview URL", lambda: (bool(parsed["init_data"]) and parsed["origin"] == "https://game.example.com", ""))
    ctx = {"base_url": "https://api.example.com", "token": "XYZ"}
    check("шаблонизация {{var}}",
          lambda: (tma_mod.render_template("{{base_url}}/x?t={{token}}", ctx) == "https://api.example.com/x?t=XYZ", ""))
    check("json_path", lambda: (tma_mod.json_path({"a": {"b": [10, 20]}}, "a.b[1]") == 20, ""))
    check("запуск пустого конвейера шагов", lambda: (tma_mod.run_steps([], {})["ok"] is True, ""))

    print("selftest: пресет Doomsday (Firebase callable)")
    # реальный формат webview URL игры: hash и session_hash из tgWebAppData
    wv = ("https://telegram-miracle-f1779.web.app/#tgWebAppData="
          "user%3D%257B%2522id%2522%253A1%257D%26auth_date%3D1700000000"
          "%26hash%3Dabc123deadbeef&tgWebAppVersion=9.6")
    bctx = tma_mod.build_context({"tma": {"base_url": ""}}, wv)
    check("session_hash из tgWebAppData",
          lambda: (bctx["session_hash"] == "abc123deadbeef", bctx["session_hash"]))
    steps = tma_mod.get_steps({"tma": {}}, "reboot")
    check("пресет шагов ребута", lambda: (bool(steps) and steps[0]["url"].endswith("/rebootProduction"), ""))
    rendered = tma_mod.render_template(steps[0]["body"], {
        "init_data": "user=1&hash=abc", "session_hash": "abc"})
    check("тело запроса callable",
          lambda: (rendered == {"data": {"auth": "user=1&hash=abc", "session": "abc"}}, ""))
    check("кастомные шаги важнее пресета",
          lambda: (tma_mod.get_steps({"tma": {"steps_reboot": [{"url": "x"}]}}, "reboot") == [{"url": "x"}], ""))

    print("selftest: парсер состояния Doomsday")
    fake_state = {
        "passiveFarm": {"active": True, "endsAt": (__import__("time").time() + 3600) * 1000},
        "gameStats": {"mines": {
            "start_res1": {"levelStore": 2, "store": {"count": 9900}, "usagePerMinute": 1,
                            "passive": {"workerCount": 5, "craftPerMinute": 2,
                                        "craftPerMinuteReal": 2, "progress": 0}},
            "uranus": {"levelStore": 0, "store": {"count": 118800}, "usagePerMinute": 0,
                        "passive": {"workerCount": 3, "isPaused": False,
                                    "craftPerMinute": 1, "craftPerMinuteReal": 1}},
            "locked": {"store": {"count": 0}},
        }},
    }
    rows = engine.parse_resources_doomsday(fake_state)
    by_name = {r["name"]: r for r in rows}
    check("ресурсы Doomsday из initUser", lambda: (len(rows) == 2 and "Уголь" in by_name, str(len(rows))))
    coal = by_name.get("Уголь") or {}
    check("русские имена и id ресурсов",
          lambda: (coal.get("id") == "start_res1" and (by_name.get("Уран") or {}).get("id") == "uranus",
                   str(coal.get("id"))))
    check("ёмкость по levelStore", lambda: (coal.get("max") == 10000.0, str(coal.get("max"))))
    check("склад полон определяется",
          lambda: ((by_name.get("Уран") or {}).get("state") == "склад переполнен",
                   str((by_name.get("Уран") or {}).get("state"))))

    print("selftest: каталог и русские названия")
    from . import game_data as gd
    check("перевод есть для всех ресурсов",
          lambda: (set(gd.RU_NAMES) == set(gd.NAMES), ""))
    check("rid_by_name (рус/англ)",
          lambda: (gd.rid_by_name("Сплав") == "alloy1" and gd.rid_by_name("Alloy") == "alloy1"
                   and gd.rid_by_name("Сoal") == "start_res1", ""))
    cat = gd.catalog()
    check("каталог ресурсов полный",
          lambda: (len(cat) >= 54 and all(c.get("id") and c.get("name") for c in cat), str(len(cat))))

    print("selftest: продажа/обмен (sellItem)")
    check("продающиеся ресурсы известны",
          lambda: (set(gd.SELL_INFO) <= set(gd.NAMES) and len(gd.SELL_INFO) == 6,
                   ", ".join(gd.SELL_INFO)))
    check("цены продажи положительные",
          lambda: (all(v["price"] > 0 for v in gd.SELL_INFO.values()), ""))
    check("sell_unit/fmt_bytes",
          lambda: (gd.sell_unit("hdd") == (12582912, False)
                   and gd.sell_unit("uran_pills") == (1, True)
                   and gd.sell_unit("iron") is None
                   and gd.fmt_bytes(525 * 12582912).endswith("ГиБ"),
                   gd.fmt_bytes(525 * 12582912)))
    sell_cat = gd.sellable_catalog()
    check("каталог продажи для панели",
          lambda: (len(sell_cat) == 6 and all(c.get("unit_ru") for c in sell_cat), ""))
    ex_steps = tma_mod.exchange_steps("floppy", 12000)
    body = ex_steps[0]["body"]
    check("шаг sellItem собирается",
          lambda: (ex_steps[0]["url"].endswith("/sellItem")
                   and body["data"]["resourceId"] == "floppy"
                   and body["data"]["amount"] == 12000
                   and isinstance(body["data"]["amount"], int), str(body["data"])))
    rendered = tma_mod.render_template(body, {"init_data": "user=1&hash=abc",
                                              "session_hash": "abc"})
    check("число не превращается в строку",
          lambda: (rendered["data"]["amount"] == 12000
                   and isinstance(rendered["data"]["amount"], int)
                   and rendered["data"]["auth"] == "user=1&hash=abc", ""))

    print("selftest: графические ассеты игры (иконки/шрифты)")
    import os as _os
    web_items = _os.path.join(paths.WEB_DIR, "assets", "items")
    missing_icons = [c["id"] for c in cat
                     if not _os.path.isfile(_os.path.join(web_items, c["id"] + ".webp"))]
    check("иконка есть для каждого ресурса",
          lambda: (not missing_icons, "нет: " + ", ".join(missing_icons[:6]) if missing_icons else ""))
    web_fonts = _os.path.join(paths.WEB_DIR, "assets", "fonts")
    fonts_ok = all(_os.path.isfile(_os.path.join(web_fonts, f))
                   for f in ("europe-normal.woff", "europe-bold.woff", "Iosevka-Regular.woff2"))
    check("шрифты Europe/Iosevka на месте", lambda: (fonts_ok, ""))

    print("selftest: чистка legacy-конфига")
    legacy = {"updater": {"enabled": True}, "schedules": {"update_check_minutes": 30},
              "notify": {"events": {"update_applied": True}}}
    stripped = cfgmod._strip_legacy(cfgmod._deep_merge(cfgmod.DEFAULTS, legacy))
    check("legacy-ключи вычищаются",
          lambda: ("updater" not in stripped
                   and "update_check_minutes" not in stripped["schedules"]
                   and "update_applied" not in stripped["notify"]["events"], ""))

    print("selftest: БД")
    with tempfile.TemporaryDirectory() as tmp:
        old_db, old_app = paths.DB_PATH, paths.APP_DIR
        try:
            paths.DB_PATH = os.path.join(tmp, "test.db")
            db._conn = None
            db.kv_set("probe", [1, 2])
            check("kv get/set", lambda: (db.kv_get("probe") == [1, 2], ""))
            db.event("scan", "тест", "тело")
            check("events", lambda: (len(db.events_list(1)) == 1, ""))
            db.resource_snapshot([{"name": "Alloy", "current": 5, "max": 10, "state": ""}])
            db.resource_snapshot([{"name": "Сплав", "current": 7, "max": 10, "state": "",
                                   "id": "alloy1"}])
            merged = db.resources_latest_ru()
            check("склейка англ+рус записей", lambda: (len(merged) == 1 and merged[0]["name"] == "Сплав"
                                                    and merged[0]["current"] == 7
                                                    and merged[0].get("id") == "alloy1",
                                                    str(merged)))

            print("selftest: ребут по циклу и фильтр уведомлений")
            import time as _time
            now = datetime.datetime.now()
            cfg_t = cfgmod._deep_merge(cfgmod.DEFAULTS, {})
            check("нет данных о цикле — запасной интервал",
                  lambda: (_reboot_due_cycle(cfg_t, now) is True, ""))
            ends_ms = lambda sec: str(int((_time.time() + sec) * 1000))
            db.kv_set("passive_farm_ends_at", ends_ms(2 * 3600))
            check("цикл далеко — ребут не нужен", lambda: (_reboot_due_cycle(cfg_t, now) is False, ""))
            db.kv_set("passive_farm_ends_at", ends_ms(5 * 60))
            check("за 10 мин до конца — пора ребутить", lambda: (_reboot_due_cycle(cfg_t, now) is True, ""))
            db.kv_set("passive_farm_ends_at", ends_ms(-30))
            check("цикл истёк — ребут немедленно", lambda: (_reboot_due_cycle(cfg_t, now) is True, ""))
            db.kv_set("cycle_reboot_guard", db.kv_get("passive_farm_ends_at"))
            check("guard: этот цикл уже перезапущен",
                  lambda: (_reboot_due_cycle(cfg_t, now) is False, ""))
            db.kv_set("passive_farm_ends_at", ends_ms(2 * 3600))
            check("новый цикл после ребута — guard не мешает",
                  lambda: (_reboot_due_cycle(cfg_t, now) is False, ""))

            db.kv_set("passive_farm_ends_at", None)
            db.kv_set("cycle_reboot_guard", None)
            sent = []
            orig_notify = engine.notify_mod.notify
            engine.notify_mod.notify = lambda cfg, kind, title, text, **kw: sent.append(title) or True
            try:
                res2 = [
                    {"name": "Сплав", "id": "alloy1", "current": 100, "max": 100, "pct": 100,
                     "state": "склад переполнен"},
                    {"name": "Уран", "id": "uranus", "current": 95, "max": 100, "pct": 95,
                     "state": ""},
                ]
                cfg_f = cfgmod._deep_merge(cfgmod.DEFAULTS, {"notify": {"resource_filter": ["alloy1"]}})
                engine._check_thresholds(cfg_f, res2)
                check("фильтр: уведомление только по выбранному",
                      lambda: (sent == ["Склад переполнен: Сплав"], str(sent)))
                sent.clear()
                db.kv_set("notified_full", [])
                db.kv_set("notified_warn", [])
                engine._check_thresholds(cfgmod._deep_merge(cfgmod.DEFAULTS, {}), res2)
                check("без фильтра — уведомления по всем",
                      lambda: (len(sent) == 2, str(sent)))

                print("selftest: план автообмена")
                res3 = [
                    {"name": "Дискета", "id": "floppy", "current": 29000, "max": 31000,
                     "pct": 93.5, "state": ""},
                    {"name": "Жёсткий диск", "id": "hdd", "current": 400, "max": 10500,
                     "pct": 3.8, "state": ""},
                    {"name": "Кассета", "id": "cassete", "current": 58000, "max": 58000,
                     "pct": 100.0, "state": "склад переполнен"},
                    {"name": "Урановые таблетки", "id": "uran_pills", "current": 0.4,
                     "max": 24, "pct": 1.7, "state": ""},
                ]
                plan = engine._exchange_plan(cfgmod.DEFAULTS, res3)
                plan_map = {rid: amt for rid, amt, _r in plan}
                check("cap: близкие к капу продаются всё",
                      lambda: (plan_map.get("floppy") == 29000 and plan_map.get("cassete") == 58000,
                               str(plan_map)))
                check("cap: далёкие от капа не трогаются",
                      lambda: ("hdd" not in plan_map, ""))
                check("always: дробный остаток меньше min не продаётся",
                      lambda: ("uran_pills" not in plan_map, str(plan_map.get("uran_pills"))))
                res3[3]["current"] = 3.0
                plan2 = engine._exchange_plan(cfgmod.DEFAULTS, res3)
                check("always: накопилось ≥ min — продаётся",
                      lambda: ({r: a for r, a, _ in plan2}.get("uran_pills") == 3.0, ""))
                cfg_keep = cfgmod._deep_merge(cfgmod.DEFAULTS,
                                              {"exchange": {"rules": [
                                                  {"rid": "floppy", "mode": "cap",
                                                   "threshold_pct": 50, "keep": 5000,
                                                   "min": 1, "enabled": True}]}})
                plan3 = engine._exchange_plan(cfg_keep, res3)
                check("keep: оставляем запас на складе",
                      lambda: ({r: a for r, a, _ in plan3}.get("floppy") == 24000, ""))
                cfg_off = cfgmod._deep_merge(cfgmod.DEFAULTS, {"exchange": {"enabled": False}})
                check("обмен выключен — план пуст",
                      lambda: (engine._exchange_plan(cfg_off, res3) == [], ""))
                check("валидация ловит мусорные правила",
                      lambda: (any("не продаётся" in e or "mode" in e
                                   for e in cfgmod.validate(cfgmod._deep_merge(
                                       cfgmod.DEFAULTS, {"exchange": {"rules": [
                                           {"rid": "iron", "mode": "cap", "threshold_pct": 90,
                                            "keep": 0, "min": 1, "enabled": True}]}}))), ""))
                check("валидация принимает дефолтные правила",
                      lambda: (not [e for e in cfgmod.validate(cfgmod.DEFAULTS)
                                    if e.startswith("exchange")], ""))
            finally:
                engine.notify_mod.notify = orig_notify
        finally:
            paths.DB_PATH, paths.APP_DIR = old_db, old_app
            db._conn = None

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {failures}")
        return 1
    print("Все проверки пройдены ✔")
    return 0


# ---------------- входная точка ----------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="doomsday", description="Doomsday Tyranny Bot для Termux")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cron", help="плановый проход (для cronie)")
    sub.add_parser("scan", help="скан ресурсов сейчас")
    sub.add_parser("exchange", help="скан + автообмен сейчас (продать по правилам)")
    sub.add_parser("reboot", help="ребут производства сейчас")
    sub.add_parser("summary", help="сводка дня сейчас")
    sub.add_parser("discover", help="обнаружить API игры")
    sub.add_parser("check", help="диагностика")
    sub.add_parser("test-notify", help="тест push-уведомления")
    sub.add_parser("login", help="вход в Telegram (интерактивно)")
    sub.add_parser("setup", help="мастер настройки")
    sub.add_parser("status", help="краткий статус и таймеры")
    lg = sub.add_parser("log", help="хвост лога")
    lg.add_argument("-n", "--lines", type=int, default=50)
    sub.add_parser("web", help="запустить веб-панель (обычно — сервисом)")
    sub.add_parser("web-restart", help="перезапустить веб-панель (убить все её процессы и поднять)")
    sub.add_parser("panel", help="открыть веб-панель в браузере")
    sub.add_parser("selftest", help="самопроверка без Telegram")
    sub.add_parser("start", help="стартёр: обновить из git и запустить всё")
    sub.add_parser("stop", help="остановить веб-панель и плановые проходы")
    lp = sub.add_parser("logs-push", aliases=["logspush"],
                        help="выгрузить логи/отчёты в ветку logs репозитория")
    lp.add_argument("-m", "--message", default="", help="подпись к выгрузке")
    ga = sub.add_parser("git-auth", help="сохранить GitHub PAT (git без пароля)")
    ga.add_argument("--token", default="", help="токен (иначе — скрытый ввод)")
    args = p.parse_args(argv)

    setup_logging(verbose=os.environ.get("DOOMSDAY_VERBOSE") == "1")

    wait_lock = args.cmd in ("reboot", "scan", "cron")
    no_lock = {"web": cmd_web, "panel": cmd_panel, "selftest": cmd_selftest, "setup": cmd_setup,
               "status": cmd_status, "log": cmd_log, "test-notify": cmd_test_notify,
               "start": cmd_start, "stop": cmd_stop, "logs-push": cmd_logs_push,
               "logspush": cmd_logs_push, "git-auth": cmd_git_auth,
               "web-restart": cmd_web_restart}
    if args.cmd in no_lock:
        return no_lock[args.cmd](args)

    with SingleInstance(wait=wait_lock):
        return {
            "cron": cmd_cron, "scan": cmd_scan, "reboot": cmd_reboot,
            "summary": cmd_summary, "discover": cmd_discover, "check": cmd_check,
            "login": cmd_login, "exchange": cmd_exchange,
        }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
