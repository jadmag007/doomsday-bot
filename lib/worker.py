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
from . import timing

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
    """Настало ли время действия: джиттерный интервал истёк и не слишком рано после ошибки."""
    sch = cfg.get("schedules", {})
    forced = False
    if name == "reboot":
        interval = timing.fallback_reboot_interval_sec(cfg, _iso_raw("last_reboot_ts"))
        last_ok = _ts("last_reboot_ts")
    elif name == "scan":
        interval = timing.scan_interval_sec(cfg, _iso_raw("last_scan_ts"))
        last_ok = _ts("last_scan_ts")
        # контрольный скан после ребута — раньше регулярного интервала
        force_at = _ts("force_scan_at")
        forced = force_at is not None and now >= force_at
    else:
        return True
    if not forced and last_ok is not None and (now - last_ok).total_seconds() < interval:
        return False
    # троттлинг повторов при ошибках
    retry = int(sch.get("retry_failed_minutes", 20) or 20) * 60
    last_fail = _ts(f"last_fail_{name}")
    if last_fail is not None and (now - last_fail).total_seconds() < retry:
        return False
    return True


def _iso_raw(key: str):
    """Сырое значение kv-таймстемпа (seed для джиттера, без парсинга)."""
    return db.kv_get(key)


def _reboot_due_cycle(cfg: dict, now) -> bool:
    """Пора ли ребутить производство — по игровому циклу.

    Основной режим: окно открывается за reboot_before_end минут до конца цикла
    (passive_farm_ends_at из скана/ребута) со стабильным джиттером ±сек —
    фарм не прерывается. Если цикл уже истёк — ребут немедленно (окно пропущено).
    Если данных о цикле нет — запасная логика по джиттерному интервалу.
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
    before_sec = timing.reboot_before_sec(cfg, ends_raw)
    if now.timestamp() < ends_sec - before_sec:
        return False  # окно ребута ещё не открылось
    # окно открыто (или цикл истёк) — но не чаще, чем разрешает троттлинг ошибок
    retry = int(sch.get("retry_failed_minutes", 20) or 20) * 60
    # 17.09 (диагностика 16.09 20:44→20:54): цикл истёк, а из-за 20-минутного
    # троттлинга ошибок ребут откладывался — производство стояло. При истёкшем
    # цикле каждая минута простоя — потерянный фарм: ретраи каждые 2 минуты.
    if now.timestamp() > ends_sec:
        retry = 120
    last_fail = _ts("last_fail_reboot")
    if last_fail is not None and (now - last_fail).total_seconds() < retry:
        return False
    return True


def _reboot_wait_sec(cfg: dict, now) -> float:
    """Сколько секунд до открытия окна ребута (0 — уже открыто/истекло).

    Нужно для ТОЧНОГО срабатывания: cron ходит каждые 10 минут, и без этого
    окно «за 10 мин до конца» фактически стреляло на следующем проходе —
    впритык к концу цикла. Проход, который видит, что окно откроется в течение
    10 минут, засыпает до точного момента (с джиттером) и ребутит вовремя.
    """
    ends_raw = db.kv_get("passive_farm_ends_at")
    try:
        ends_sec = float(ends_raw) / 1000.0 if ends_raw else None
    except (TypeError, ValueError):
        return 0.0
    if ends_sec is None:
        return 0.0
    before_sec = timing.reboot_before_sec(cfg, ends_raw)
    wait = (ends_sec - before_sec) - now.timestamp()
    if wait <= 0 or wait > 570:  # дальше — пусть стреляет следующий проход
        return 0.0
    return wait


def _fmt_delta(sec) -> str:
    if sec is None:
        return "нет данных"
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return (f"{h}ч {m:02d}м" if h else f"{m} мин") + (" назад" if sec >= 0 else "")


# ---------------- cron-планировщик ----------------

def _ensure_wake_lock() -> None:
    """Пере-взять wake-lock (идемпотентно).

    Логи устройства 2026-09-15: между проходами cron провалы 43–90 минут —
    Android Doze усыпляет Termux, даже если wake-lock брался при старте
    (Termux мог перезапуститься, lock слетел). Каждый проход продлеваем.
    termux-wake-lock входит в termux-tools, приложение termux-api не нужно.
    """
    try:
        import shutil as _sh
        import subprocess as _sp
    except ImportError:
        return
    try:
        wl = _sh.which("termux-wake-lock")
        if wl:
            _sp.run([wl], capture_output=True, timeout=10)
    except (OSError, _sp.SubprocessError):
        pass


def _cron_watchdog(seconds: int = 900) -> None:
    """Сторожевой таймер прохода cron: зависший проход убивается через N секунд.

    Раньше зависший (например, на сети) проход держал блокировку вечно —
    следующие проходы стояли в очереди, и ни ребут, ни скан не выполнялись.
    SIGALRM прерывает даже висящий сетевой вызов; процесс умирает, лок
    освобождается, следующий проход (через 10 мин) работает.
    """
    import signal

    def _boom(signum, frame):
        raise TimeoutError(f"проход cron прерван сторожевым таймером ({seconds} с)")

    try:
        signal.signal(signal.SIGALRM, _boom)
        signal.alarm(seconds)
    except (ValueError, OSError):
        pass


def cmd_cron(args) -> int:
    cfg = cfgmod.load()
    now = datetime.datetime.now()
    db.kv_set("last_cron_ts", db.now_iso())
    _ensure_wake_lock()
    _cron_watchdog(900)

    # ночной режим: плановых действий нет, бот «спит»
    if timing.night_active(cfg, now):
        if db.kv_get("night_skip_date") != now.date().isoformat():
            db.kv_set("night_skip_date", now.date().isoformat())
            nm = (cfg.get("security") or {}).get("night_mode") or {}
            db.event("cron", "Ночной режим",
                     f"Плановые действия приостановлены до утра "
                     f"(окно {nm.get('from')}–{nm.get('to')})")
            print(f"[cron] {db.now_iso()} ночной режим — плановые действия пропущены")
        return 0

    actions = []
    # 1) ребут производства — по игровому циклу (за N минут до конца)
    if _reboot_due_cycle(cfg, now):
        actions.append("reboot")
    # 2) скан ресурсов
    if _due(now, cfg, "scan"):
        actions.append("scan")
    # 3) сводка дня (время — со стабильным джиттером по дню)
    st = str(cfg.get("schedules", {}).get("summary_time") or "20:00")
    m = re.match(r"^(\d{1,2}):(\d{2})$", st)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        off = timing.summary_minute_offset(cfg, now.date().isoformat())
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0) \
            + datetime.timedelta(minutes=off)
        if now >= due and db.kv_get("last_summary_date") != now.date().isoformat():
            actions.append("summary")

    # точное срабатывание ребута: если окно откроется до следующего прохода —
    # дожидаемся его в этом проходе (гранулярность cron больше не решает)
    if "reboot" not in actions:
        wait = _reboot_wait_sec(cfg, now)
        if wait > 0:
            print(f"[cron] окно ребута через {int(wait)} с — ожидаю точный момент…")
            try:
                import time as _t
                _t.sleep(wait)
            except (TimeoutError, InterruptedError):
                pass
            if _reboot_due_cycle(cfg, datetime.datetime.now()):
                actions.insert(0, "reboot")

    print(f"[cron]{' [fallback]' if os.environ.get('DOOMSDAY_CRON_FALLBACK') else ''} "
          f"{db.now_iso()} план: {actions or 'ничего не подошло'}")
    for name in actions:
        try:
            r = _run_action_logged(cfg, name)
            if r.get("ok", True):
                db.kv_set(f"last_fail_{name}", None)
            else:
                db.kv_set(f"last_fail_{name}", db.now_iso())
        except Exception as e:
            db.kv_set(f"last_fail_{name}", db.now_iso())
            log.exception("Действие %s провалилось: %s", name, e)
            notify_mod.notify_error(cfg, f"Ошибка: {name}", str(e)[:300])
            db.kv_set("last_error_ts", db.now_iso())
    _prune_run_logs()
    return 0


def _run_action(cfg, name: str) -> dict:
    if name == "reboot":
        return engine.run_coro(engine.action_reboot(cfg))
    if name == "scan":
        return engine.run_coro(engine.action_scan(cfg))
    if name == "summary":
        return engine.run_coro(engine.action_summary(cfg))
    raise ValueError(f"Неизвестное действие: {name}")


def _run_action_logged(cfg, name: str) -> dict:
    """Действие cron-прохода с журналированием результата в run-<id>.log.

    Раньше run-логи писали только действия из веб-панели: cron-проходы
    оставляли лишь план в cron.log, и по свежим логам устройства не было
    видно, что именно скан продал и что ответил сервер (диагностика 15.09
    20:05 — обмен состоялся, но его JSON-результат нигде не сохранился).
    Теперь каждый cron-скан/ребут/сводка пишет тот же run-лог, что и
    ручные запуски: он и в панели (Запуски), и в бандлах logs-push.
    """
    rid = db.run_start(f"cron:{name}")
    log_path = os.path.join(paths.LOG_DIR, f"run-{rid}.log")
    try:
        r = _run_action(cfg, name)
        ok = bool(r.get("ok", True))
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        except OSError:
            pass
        db.run_finish(rid, "ok" if ok else "fail",
                      json.dumps(r, ensure_ascii=False, default=str)[:16000])
        return r
    except Exception as e:
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"провалено: {e!r}\n")
        except OSError:
            pass
        db.run_finish(rid, "fail", repr(e)[:2000])
        raise


def _prune_run_logs(keep: int = 20) -> None:
    """Держать только последние `keep` run-*.log — теперь их пишут и cron-проходы,
    без пруна бандлы logs-push разбухнут."""
    import glob as _glob
    try:
        files = sorted(_glob.glob(os.path.join(paths.LOG_DIR, "run-*.log")),
                       key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0)
        for p in files[:-keep] if len(files) > keep else []:
            os.unlink(p)
    except OSError:
        pass


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
            extra = (f", запрошено: {x['requested']:g}"
                     if x.get("requested") is not None and (x.get("requested") or 0) - (x.get("amount") or 0) >= 1
                     else "")
            print(f"  - {x['name']} ×{x['amount']:g} → {x['proceeds']} "
                  f"(остаток: {x['left']:g}, баланс: {x['balance_ru']}{extra})")
    else:
        print("По правилам обмена продавать нечего.")
    print(json.dumps({"ok": r.get("ok"), "resources": len(r.get("resources") or [])},
                     ensure_ascii=False))
    return 0 if r.get("ok") else 1


def cmd_sell_all(args) -> int:
    """Собрать всю память: скан + продать ВСЕ байтовые носители сейчас.

    Мимо правил/порогов/переключателя автообмена — ручное действие из
    панели («Собрать всю память»). DDT-ресурсы не трогаем.
    """
    cfg = cfgmod.load()
    r = engine.run_coro(engine.action_scan(cfg, sell_all=True))
    ex = r.get("exchange") or []
    if ex:
        print("Собрано памяти:")
        for x in ex:
            extra = (f", запрошено: {x['requested']:g}"
                     if x.get("requested") is not None and (x.get("requested") or 0) - (x.get("amount") or 0) >= 1
                     else "")
            print(f"  - {x['name']} ×{x['amount']:g} → {x['proceeds']} "
                  f"(остаток: {x['left']:g}, баланс: {x['balance_ru']}{extra})")
    else:
        print("Памяти для продажи нет: байтовые носители на складах отсутствуют.")
    print(json.dumps({"ok": r.get("ok"), "resources": len(r.get("resources") or []),
                      "sell_all": True}, ensure_ascii=False))
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
    eta = db.kv_get("ddt_eta") or {}
    for rid, it in eta.items():
        if it.get("eta_sec") is not None:
            print(f"До следующей ед. {game_data.ru_name(rid, rid)}: "
                  f"{_fmt_delta(it['eta_sec'])}")
    last_cron = _ts("last_cron_ts")
    print(f"Последний проход cron: {_fmt_delta((now - last_cron).total_seconds()) if last_cron else 'нет данных'}")
    if timing.night_active(cfg, now):
        nm = (cfg.get("security") or {}).get("night_mode") or {}
        print(f"Ночной режим:         АКТИВЕН (до {nm.get('to')}) — плановых действий нет")
    elif ((cfg.get("security") or {}).get("night_mode") or {}).get("enabled"):
        nm = cfg["security"]["night_mode"]
        print(f"Ночной режим:         вкл, окно {nm.get('from')}–{nm.get('to')}")
    jt = ((cfg.get("security") or {}).get("jitter") or {})
    if jt.get("enabled", True):
        print(f"Джиттер интервалов:   вкл (скан ±{jt.get('scan_percent', 15)}%, "
              f"ребут ±{jt.get('reboot_seconds', 180)} с)")
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
    # сессию игры регистрирует только initUser — он ОБЯЗАН идти перед rebootProduction
    # (диагностика 2026-09-15: без этого 403 "Expired session")
    check("пресет ребута: initUser первый",
          lambda: (bool(steps) and steps[0]["name"] == "initUser", str([s.get("name") for s in steps])))
    check("пресет ребута: rebootProduction второй",
          lambda: (len(steps) > 1 and steps[1]["url"].endswith("/rebootProduction"), ""))
    check("initUser в ребуте извлекает конец цикла",
          lambda: ("farm_ends_at" in (steps[0].get("extract") or {}), ""))
    rendered = tma_mod.render_template(steps[1]["body"], {
        "init_data": "user=1&hash=abc", "session_hash": "abc"})
    check("тело запроса callable",
          lambda: (rendered == {"data": {"auth": "user=1&hash=abc", "session": "abc"}}, ""))
    check("кастомные шаги важнее пресета",
          lambda: (tma_mod.get_steps({"tma": {"steps_reboot": [{"url": "x"}]}}, "reboot") == [{"url": "x"}], ""))

    print("selftest: протокол сессий ребута (регрессия 2026-09-15)")
    _t = __import__("time").time()
    check("свежий цикл (12 ч) — ребут не нужен",
          lambda: (engine.reboot_fresh_cycle((_t + 43200) * 1000, _t, 600) is True, ""))
    check("окно ребута (10 мин) — ребут нужен",
          lambda: (engine.reboot_fresh_cycle((_t + 600) * 1000, _t, 600) is False, ""))
    check("цикл истёк — ребут нужен",
          lambda: (engine.reboot_fresh_cycle((_t - 60) * 1000, _t, 600) is False, ""))
    check("нет данных о цикле — ребут нужен",
          lambda: (engine.reboot_fresh_cycle(None, _t, 600) is False, ""))
    _expired = [{"status": "fail",
                 "detail": "HTTP 403: {\"error\":{\"details\":{\"status\":418},"
                           "\"message\":\"Expired session\",\"status\":\"PERMISSION_DENIED\"}}"}]
    check("детектор отказа сессии (Expired session/403)",
          lambda: (engine._is_session_error(_expired) is True, ""))
    check("детектор не срабатывает на другие ошибки",
          lambda: (engine._is_session_error([{"status": "fail", "detail": "HTTP 500: boom"}]) is False, ""))
    check("детектор игнорирует успешные шаги",
          lambda: (engine._is_session_error([{"status": "ok", "detail": "HTTP 200"}]) is False, ""))

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

            print("selftest: диагностика логов (регрессия 15.09 20:05)")
            # cron-проходы обязаны оставлять run-лог с JSON-результатом
            old_log_dir, old_rep_dir = paths.LOG_DIR, paths.REPORTS_DIR
            paths.LOG_DIR = os.path.join(tmp, "logs")
            paths.REPORTS_DIR = os.path.join(tmp, "reports")
            os.makedirs(paths.LOG_DIR, exist_ok=True)
            os.makedirs(paths.REPORTS_DIR, exist_ok=True)
            orig_ra = globals()["_run_action"]
            globals()["_run_action"] = lambda cfg, name: {"ok": True, "probe": name}
            try:
                r = _run_action_logged({}, "scan")
                _rid = db.runs_list(limit=1)[0]["id"]
                _lp = os.path.join(paths.LOG_DIR, f"run-{_rid}.log")
                check("cron-действие пишет run-лог с JSON",
                      lambda: (r.get("probe") == "scan"
                               and os.path.isfile(_lp)
                               and '"probe": "scan"' in open(_lp, encoding="utf-8").read(),
                               _lp))
                _run = db.runs_list(limit=1)[0]
                check("запуск числится в журнале запусков",
                      lambda: (_run["action"] == "cron:scan" and _run["status"] == "ok", str(_run)))
                globals()["_run_action"] = lambda cfg, name: (_ for _ in ()).throw(RuntimeError("boom"))
                try:
                    _run_action_logged({}, "scan")
                    _raised = False
                except RuntimeError:
                    _raised = True
                check("исключение действия фиксируется как fail",
                      lambda: (_raised and db.runs_list(limit=1)[0]["status"] == "fail",
                               str(db.runs_list(limit=1)[0])))
            finally:
                globals()["_run_action"] = orig_ra
            # прун run-логов и очистка отправленного — в чистом каталоге
            paths.LOG_DIR = os.path.join(tmp, "logs2")
            os.makedirs(paths.LOG_DIR, exist_ok=True)
            import glob as _glob
            for i in range(25):
                p = os.path.join(paths.LOG_DIR, f"run-{900 + i}.log")
                with open(p, "w") as f:
                    f.write("x")
                os.utime(p, (1700000000 + i, 1700000000 + i))
            _prune_run_logs()
            check("прун оставляет последние 20 run-логов",
                  lambda: (len(_glob.glob(os.path.join(paths.LOG_DIR, "run-*.log"))) == 20, ""))
            from . import logspush as _lpm
            # v2.4.4: точная обрезка отправленного по снапшоту (inode+размер)
            # вместо окна «моложе 5 минут не трогать»
            import tempfile as _tf
            with _tf.TemporaryDirectory() as _td:
                _f1 = os.path.join(_td, "a.log")
                with open(_f1, "wb") as f:
                    f.write(b"line1\nline2\n")
                _st1 = os.stat(_f1)
                with open(_f1, "ab") as f:  # писатель дописал ПОСЛЕ снапшота
                    f.write(b"line3\n")
                check("trim: дозаписанное после снапшота сохранено",
                      lambda: (_lpm._trim_consumed(_f1, _st1.st_ino, _st1.st_size) == "trimmed"
                               and open(_f1, "rb").read() == b"line3\n", ""))
                _f2 = os.path.join(_td, "b.log")
                with open(_f2, "wb") as f:
                    f.write(b"x\n")
                _st2 = os.stat(_f2)
                check("trim: неизменный лог обрезан до пустого",
                      lambda: (_lpm._trim_consumed(_f2, _st2.st_ino, _st2.st_size) == "trimmed"
                               and os.path.getsize(_f2) == 0, ""))
                _f3 = os.path.join(_td, "c.log")
                with open(_f3, "wb") as f:
                    f.write(b"new\n")
                check("trim: ротированный (другой inode) не тронут",
                      lambda: (_lpm._trim_consumed(_f3, 999999, 0) == "rotated"
                               and open(_f3, "rb").read() == b"new\n", ""))
                _st3b = os.stat(_f3)
                check("trim: без снапшота — статус shrunk, файл цел",
                      lambda: (_lpm._trim_consumed(_f3, _st3b.st_ino, None) == "shrunk"
                               and open(_f3, "rb").read() == b"new\n", ""))
                check("trim: отсутствующий файл — gone",
                      lambda: (_lpm._trim_consumed(os.path.join(_td, "nope.log"), 1, 0) == "gone", ""))
            _old = os.path.join(paths.LOG_DIR, "bot.log")
            with open(_old, "w") as f:
                f.write("старые строки")
            _snap_old = os.stat(_old)
            _new = os.path.join(paths.LOG_DIR, "cron.log")   # свежий по mtime — больше не помеха
            with open(_new, "w") as f:
                f.write("свежее")
            _snap_new = os.stat(_new)
            _rot = os.path.join(paths.LOG_DIR, "bot.log.1")
            with open(_rot, "w") as f:
                f.write("ротационный архив")
            _snap_rot = os.stat(_rot)
            _rep = os.path.join(paths.REPORTS_DIR, "discovery-x.json")
            with open(_rep, "w") as f:
                f.write("{}")
            cleared = _lpm._clear_pushed_logs({
                "logs": ["bot.log", "cron.log", "bot.log.1"],
                "log_state": {
                    "bot.log": {"ino": _snap_old.st_ino, "size": _snap_old.st_size},
                    "cron.log": {"ino": _snap_new.st_ino, "size": _snap_new.st_size},
                    "bot.log.1": {"ino": _snap_rot.st_ino, "size": _snap_rot.st_size}},
                "reports": ["discovery-x.json"]})
            check("логи обрезаны сразу, без 5-минутного окна",
                  lambda: (cleared == 3 and os.path.getsize(_old) == 0
                           and os.path.getsize(_new) == 0, str(cleared)))
            check("ротационный архив после отправки удалён",
                  lambda: (not os.path.isfile(_rot), ""))
            check("отправленный отчёт удалён",
                  lambda: (not os.path.isfile(_rep), ""))
            with open(_old, "w") as f:
                f.write("данные")
            _lpm._clear_pushed_logs({"logs": ["bot.log"]})  # манифест без снапшота
            check("манифест без снапшота — лог не тронут",
                  lambda: (os.path.getsize(_old) > 0, ""))
            paths.LOG_DIR, paths.REPORTS_DIR = old_log_dir, old_rep_dir

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

            # 17.09: истёкший цикл — короткий троттлинг ошибок (2 мин вместо 20)
            db.kv_set("passive_farm_ends_at", ends_ms(-30))
            db.kv_set("cycle_reboot_guard", None)
            db.kv_set("last_fail_reboot", db.now_iso())
            check("истёкший цикл: фейл только что — короткий троттлинг держит",
                  lambda: (_reboot_due_cycle(cfg_t, datetime.datetime.now()) is False, ""))
            db.kv_set("last_fail_reboot",
                      (datetime.datetime.now() - datetime.timedelta(minutes=3))
                      .isoformat(timespec="seconds"))
            check("истёкший цикл: фейл 3 мин назад — ретрай разрешён (не 20 мин)",
                  lambda: (_reboot_due_cycle(cfg_t, datetime.datetime.now()) is True, ""))
            db.kv_set("last_fail_reboot", None)

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
                # force_all («собрать всю память»): мимо правил и выключателя
                plan_f = engine._exchange_plan(cfg_off, res3, force_all=True)
                fmap = {rid: amt for rid, amt, rule in plan_f}
                check("sell-all: все байтовые носители в плане (даже при выключенном обмене)",
                      lambda: (fmap.get("floppy") == 29000 and fmap.get("hdd") == 400
                               and fmap.get("cassete") == 58000, str(fmap)))
                check("sell-all: DDT-ресурсы не трогаются",
                      lambda: ("uran_pills" not in fmap and "u235" not in fmap
                               and "ddt_res" not in fmap, str(fmap)))
                check("sell-all: правило помечено как manual с keep=0",
                      lambda: (all(rule.get("mode") == "manual" and rule.get("keep") == 0
                                   for _rid, _amt, rule in plan_f), ""))
                res4 = [dict(r) for r in res3 if r["id"] != "hdd"]
                check("sell-all: пустые склады пропускаются",
                      lambda: ("hdd" not in {r: a for r, a, _ in
                                             engine._exchange_plan(cfgmod.DEFAULTS, res4,
                                                                   force_all=True)}, ""))
                check("sell-all: пустой склад — план пуст",
                      lambda: (engine._exchange_plan(cfgmod.DEFAULTS,
                                                     [{"id": "uran_pills", "current": 5,
                                                       "max": 24, "pct": 20, "state": ""}],
                                                     force_all=True) == [], ""))
                check("валидация ловит мусорные правила",
                      lambda: (any("не продаётся" in e or "mode" in e
                                   for e in cfgmod.validate(cfgmod._deep_merge(
                                       cfgmod.DEFAULTS, {"exchange": {"rules": [
                                           {"rid": "iron", "mode": "cap", "threshold_pct": 90,
                                            "keep": 0, "min": 1, "enabled": True}]}}))), ""))
                check("валидация принимает дефолтные правила",
                      lambda: (not [e for e in cfgmod.validate(cfgmod.DEFAULTS)
                                    if e.startswith("exchange")], ""))

                print("selftest: безопасность — ночной режим и джиттер")
                from . import timing
                import datetime as _dt
                cfg_n = cfgmod._deep_merge(cfgmod.DEFAULTS, {})
                check("ночь выключена по умолчанию",
                      lambda: (timing.night_active(cfg_n, _dt.datetime(2026, 1, 1, 3, 0)) is False, ""))
                cfg_n2 = cfgmod._deep_merge(cfgmod.DEFAULTS, {"security": {"night_mode": {
                    "enabled": True, "from": "01:00", "to": "07:00"}}})
                check("окно 01–07: в 03:00 ночь",
                      lambda: (timing.night_active(cfg_n2, _dt.datetime(2026, 1, 1, 3, 0)) is True, ""))
                check("окно 01–07: в 12:00 день",
                      lambda: (timing.night_active(cfg_n2, _dt.datetime(2026, 1, 1, 12, 0)) is False, ""))
                check("окно 01–07: в 00:30 день",
                      lambda: (timing.night_active(cfg_n2, _dt.datetime(2026, 1, 1, 0, 30)) is False, ""))
                cfg_n3 = cfgmod._deep_merge(cfgmod.DEFAULTS, {"security": {"night_mode": {
                    "enabled": True, "from": "23:00", "to": "06:00"}}})
                check("окно через полночь: 23:30 ночь",
                      lambda: (timing.night_active(cfg_n3, _dt.datetime(2026, 1, 1, 23, 30)) is True, ""))
                check("окно через полночь: 05:59 ночь",
                      lambda: (timing.night_active(cfg_n3, _dt.datetime(2026, 1, 1, 5, 59)) is True, ""))
                check("окно через полночь: 06:00 день",
                      lambda: (timing.night_active(cfg_n3, _dt.datetime(2026, 1, 1, 6, 0)) is False, ""))
                f1 = timing.jitter_factor("seed-1", 15)
                check("джиттер-фактор стабилен и в диапазоне",
                      lambda: (f1 == timing.jitter_factor("seed-1", 15) and 0.85 <= f1 <= 1.15, f"{f1:.3f}"))
                check("джиттер-фактор различается по seed",
                      lambda: (len({round(timing.jitter_factor(f"s{i}", 15), 4) for i in range(20)}) > 10, ""))
                check("джиттер 0% — ровно 1.0",
                      lambda: (timing.jitter_factor("x", 0) == 1.0, ""))
                b1 = timing.reboot_before_sec(cfgmod.DEFAULTS, "1757900000000")
                check("окно ребута с джиттером в диапазоне 7–13 мин",
                      lambda: (7 * 60 <= b1 <= 13 * 60, f"{b1:.0f} с"))
                cfg_tight = cfgmod._deep_merge(cfgmod.DEFAULTS, {
                    "schedules": {"reboot_before_end_minutes": 1},
                    "security": {"jitter": {"enabled": True, "reboot_seconds": 600}}})
                check("окно ребута не ближе 60 с к концу цикла",
                      lambda: (timing.reboot_before_sec(cfg_tight, "1757900000000") >= 60, ""))
                cfg_off = cfgmod._deep_merge(cfgmod.DEFAULTS, {"security": {"jitter": {"enabled": False}}})
                check("джиттер выключен — окно ровно 10 мин",
                      lambda: (timing.reboot_before_sec(cfg_off, "1757900000000") == 600.0, ""))
                scan_j = timing.scan_interval_sec(cfgmod.DEFAULTS, "2026-09-15T17:19:00")
                check("интервал скана с джиттером ±15%",
                      lambda: (60 * 60 * 0.85 <= scan_j <= 60 * 60 * 1.15,
                               f"{scan_j/60:.1f} мин"))
                check("валидация ловит кривое окно ночи",
                      lambda: (any("night_mode" in e for e in cfgmod.validate(cfgmod._deep_merge(
                          cfgmod.DEFAULTS, {"security": {"night_mode": {
                              "enabled": True, "from": "25:00", "to": "07:00"}}}))), ""))
                check("валидация принимает дефолтную безопасность",
                      lambda: (not [e for e in cfgmod.validate(cfgmod.DEFAULTS)
                                    if e.startswith("security")], ""))

                print("selftest: ETA DDT-ресурсов (урановые таблетки)")
                res4 = [
                    {"name": "Урановые таблетки", "id": "uran_pills", "current": 2, "max": 24,
                     "state": "", "craft_info": {"progress": 0, "craft": 1 / 60.0,
                                                 "craft_real": 0.1 / 60.0, "workers": 1}},
                    {"name": "Уран", "id": "uranus", "current": 50, "max": 118800,
                     "state": "", "craft_info": {"progress": 0, "craft": 84 / 60.0,
                                                 "craft_real": 84 / 60.0, "workers": 30}},
                ]
                eta = engine.compute_ddt_eta(res4)
                pills = eta.get("uran_pills") or {}
                # партия не начата, входы идут → первая партия идеальным темпом
                # (1/ч → 3600 с, как показывает игра при progress=0);
                # ДАЛЬНЕЙШИЕ партии — по реальной скорости 0.1/ч → 36000 с
                check("первая партия — идеальный темп (1/ч → 1 ч)",
                      lambda: (3590 <= (pills.get("eta_sec") or 0) <= 3610,
                               str(pills.get("eta_sec"))))
                check("период партий по реальной скорости (0.1/ч → 10 ч)",
                      lambda: (35990 <= (pills.get("period_real") or 0) <= 36010,
                               str(pills.get("period_real"))))
                check("идеальный период = 60*workers/craft",
                      lambda: (3590 <= (pills.get("period") or 0) <= 3610,
                               str(pills.get("period"))))
                # идущая партия завершается темпом стены (progress += elapsed
                # в runProductionMines) — дефицит тормозит только старт следующих
                res4[0]["craft_info"] = {"progress": 1800, "craft": 1 / 60.0,
                                         "craft_real": 0, "workers": 1}
                eta2 = engine.compute_ddt_eta(res4)
                pills2 = eta2.get("uran_pills") or {}
                check("идущая партия — темп стены (1800/3600 → 1800 с)",
                      lambda: (1795 <= (pills2.get("eta_sec") or 0) <= 1805,
                               str(pills2.get("eta_sec"))))
                check("стоп входов — period_real нет (одна партия)",
                      lambda: (pills2.get("period_real") is None, ""))
                # полный стоп, партия не начата: вход (уран) идёт 84/ч, на складе
                # 50 из 840 → (840-50)/84 ч ≈ 33 857 с
                res4[0]["craft_info"] = {"progress": 0, "craft": 1 / 60.0,
                                         "craft_real": 0, "workers": 1}
                eta2b = engine.compute_ddt_eta(res4)
                pills2b = eta2b.get("uran_pills") or {}
                check("полный стоп — ETA по доходу урана (~9.4 ч)",
                      lambda: (33840 <= (pills2b.get("eta_sec") or 0) <= 33880,
                               str(pills2b.get("eta_sec"))))
                res5 = [{"name": "Урановые таблетки", "id": "uran_pills", "current": 0, "max": 24,
                         "state": "", "craft_info": {"progress": 0, "craft": None,
                                                     "craft_real": None, "workers": 0}}]
                eta3 = engine.compute_ddt_eta(res5)
                check("простаивает — ETA нет",
                      lambda: ((eta3.get("uran_pills") or {}).get("eta_sec") is None, ""))

                print("selftest: проекция продажи DDT (fix «+1 00:00:00» из v2.5.0)")
                from . import webui as _webui
                _now = datetime.datetime.now()
                _at = _now - datetime.timedelta(minutes=50)   # скан был 50 мин назад
                _snap = {"at": _at.isoformat(timespec="seconds"), "stock": 3, "cap": 24,
                         "workers": 1, "progress": 1800.0, "period": 3600.0,
                         "period_real": 3600.0, "eta_sec": 1800, "note": ""}
                _cfg_x = {"exchange": {"enabled": True, "rules": [
                    {"rid": "uran_pills", "mode": "always", "keep": 0, "min": 1,
                     "enabled": True}]}}
                _next = _now + datetime.timedelta(minutes=10)
                _pr = _webui._ddt_sell_projection(_cfg_x, {"uran_pills": _snap},
                                                  _next, 3600, _now)
                _it = _pr.get("uran_pills") or {}
                # срез 50 мин: партия уже тикнула (+1) → скан через 10 мин продаст 4
                check("проекция: +4 на ближайшем скане (600 с)",
                      lambda: (_it.get("n") == 4.0 and _it.get("t_sec") == 600.0,
                               str(_it)))
                # старый снимок, eta давно истекла (сценарий «стоит 0 часов»)
                _snap2 = dict(_snap, at=(_now - datetime.timedelta(minutes=80)).isoformat(
                    timespec="seconds"), stock=0, eta_sec=600, period_real=600.0)
                _pr2 = _webui._ddt_sell_projection(_cfg_x, {"uran_pills": _snap2},
                                                   _now + datetime.timedelta(minutes=50),
                                                   3600, _now)
                _it2 = _pr2.get("uran_pills") or {}
                # к скану через 50 мин с 80-го мин при периоде 10 мин будет 13 шт.
                check("проекция: пересчёт вперёд (13 шт. через 3000 с)",
                      lambda: (_it2.get("n") == 13.0 and _it2.get("t_sec") == 3000.0,
                               str(_it2)))
                # стоп входов: одна партия и продажа на ближайшем скане
                _snap3 = dict(_snap, at=(_now - datetime.timedelta(minutes=5)).isoformat(
                    timespec="seconds"), stock=0, eta_sec=5400, period_real=None)
                _pr3 = _webui._ddt_sell_projection(_cfg_x, {"uran_pills": _snap3},
                                                   _next, 3600, _now)
                _it3 = _pr3.get("uran_pills") or {}
                check("проекция: стоп входов — одна партия (+1)",
                      lambda: (_it3.get("n") == 1.0, str(_it3)))
                # правило выключено — продажи нет, n = текущий склад
                _cfg_off = {"exchange": {"enabled": True, "rules": [
                    {"rid": "uran_pills", "mode": "always", "keep": 0, "min": 1,
                     "enabled": False}]}}
                _pr4 = _webui._ddt_sell_projection(_cfg_off, {"uran_pills": _snap},
                                                   _next, 3600, _now)
                _it4 = _pr4.get("uran_pills") or {}
                check("правило выкл — не продаётся (n=склад, t нет)",
                      lambda: (_it4.get("t_sec") is None and _it4.get("n") == 4
                               and "правил" in (_it4.get("note") or ""), str(_it4)))

                print("selftest: обновление панели после ребута (регрессия 15.09 19:15)")
                # сценарий устройства: цикл истёк, кассета 90.0%, ребут прошёл,
                # но скана не было — панель висела с «требуется ребут»
                _fresh_ends = int((_time.time() + 12 * 3600) * 1000)
                _mines = {
                    "cassete": {"levelStore": 29, "store": {"count": 52180}, "usagePerMinute": 0,
                                "passive": {"workerCount": 2, "craftPerMinute": 30,
                                            "craftPerMinuteReal": 30, "progress": 0}},
                }
                _state_expired = {
                    "passiveFarm": {"endsAt": int((_time.time() - 60) * 1000)},
                    "gameStats": {"coin": 1000, "mCoin": 5, "mines": _mines},
                }
                _ctx_r = {"game_state": _state_expired, "session_hash": "abc",
                          "init_data": "user=1", "base_url": "https://x"}

                def _fake_run_steps(steps, ctx, timeout=25):
                    c = dict(ctx)
                    c["game_state"] = {"passiveFarm": {"endsAt": _fresh_ends},
                                       "gameStats": {"coin": 1000, "mCoin": 5, "mines": _mines}}
                    c["farm_ends_at"] = _fresh_ends
                    c["balance_coin"] = 1000
                    c["balance_mcoin"] = 5
                    return {"ok": True, "results": [{"name": "initUser", "status": "ok",
                                                     "detail": "HTTP 200"}], "ctx": c}

                _ex_seen = []

                async def _fake_exchange(cfg, resources, ctx, timeout):
                    _ex_seen.append([(r.get("id"), r.get("state")) for r in resources])
                    return [{"rid": "cassete", "name": "Кассета", "amount": 52180,
                             "proceeds": "+313 МБ", "balance": 4194304,
                             "balance_ru": "4 ГБ", "left": 0}]

                _orig_rs, _orig_ex = engine.tma.run_steps, engine._auto_exchange
                engine.tma.run_steps = _fake_run_steps
                engine._auto_exchange = _fake_exchange
                try:
                    rep = engine.run_coro(engine._refresh_after_reboot(
                        cfgmod.DEFAULTS, _ctx_r, 25, {"name": "initUser", "url": "x"},
                        rebooted=True))
                    check("после ребута: автообмен запускается сразу",
                          lambda: (len(rep) == 1 and rep[0]["rid"] == "cassete", str(rep)))
                    _st = {r.get("id"): r.get("state") for r in db.resources_latest_ru()}
                    check("после ребута: статус «требуется ребут» снят",
                          lambda: (_st.get("cassete") == "", str(_st)))
                    check("после ребута: новый цикл сохранён",
                          lambda: (db.kv_get("passive_farm_ends_at") == str(_fresh_ends),
                                   str(db.kv_get("passive_farm_ends_at"))))
                    _fs = db.kv_get("force_scan_at")
                    check("контрольный скан назначен", lambda: (_fs is not None, str(_fs)))
                    if _fs:
                        _in_min = (_dt.datetime.fromisoformat(_fs)
                                   - _dt.datetime.now()).total_seconds() / 60
                        check("контрольный скан через 4–10 мин",
                              lambda: (4 <= _in_min <= 10, f"{_in_min:.1f} мин"))
                    # фолбэк: повторный initUser не удался — состояние до ребута
                    engine.tma.run_steps = lambda steps, ctx, timeout=25: \
                        {"ok": False, "results": [], "ctx": {}}
                    _ex_seen.clear()
                    engine.run_coro(engine._refresh_after_reboot(
                        cfgmod.DEFAULTS, _ctx_r, 25, {"name": "initUser", "url": "x"},
                        rebooted=True))
                    _st2 = {r.get("id"): r.get("state") for r in db.resources_latest_ru()}
                    check("фолбэк: статус снят и без повторного initUser",
                          lambda: (_st2.get("cassete") == "" and len(_ex_seen) == 1, str(_st2)))
                    # skip-путь (цикл уже свежий): состояние из initUser как есть
                    _ctx_skip = {"game_state": {"passiveFarm": {"endsAt": _fresh_ends},
                                                "gameStats": {"coin": 1, "mCoin": 2,
                                                              "mines": _mines}}}
                    engine.run_coro(engine._refresh_after_reboot(
                        cfgmod.DEFAULTS, _ctx_skip, 25, {"name": "initUser", "url": "x"},
                        rebooted=False))
                    check("skip (цикл свежий): обмен по свежему состоянию",
                          lambda: (len(_ex_seen) == 2, str(_ex_seen)))
                finally:
                    engine.tma.run_steps = _orig_rs
                    engine._auto_exchange = _orig_ex
                db.kv_set("last_scan_ts", _dt.datetime.now().isoformat(timespec="seconds"))
                db.kv_set("force_scan_at", None)
                check("без форса скан ждёт интервала",
                      lambda: (_due(_dt.datetime.now(), cfgmod.DEFAULTS, "scan") is False, ""))
                db.kv_set("force_scan_at",
                          (_dt.datetime.now() - _dt.timedelta(seconds=1)).isoformat(timespec="seconds"))
                check("просроченный форс — скан назначен",
                      lambda: (_due(_dt.datetime.now(), cfgmod.DEFAULTS, "scan") is True, ""))
                db.kv_set("force_scan_at",
                          (_dt.datetime.now() + _dt.timedelta(minutes=5)).isoformat(timespec="seconds"))
                check("форс в будущем — ждём", lambda: (_due(_dt.datetime.now(), cfgmod.DEFAULTS,
                                                            "scan") is False, ""))
                db.kv_set("force_scan_at", None)

                print("selftest: ежедневный бонус — цепочка 35 дней (v2.5.0)")
                from . import game_data
                check("цепочка: 35 дней",
                      lambda: (game_data.daily_chain_days() == 35,
                               str(game_data.daily_chain_days())))
                _r4 = game_data.daily_reward(4, 12)
                check("день 4 (ур.12): DDT x10",
                      lambda: (_r4["reward"] == "ddt" and _r4["amount"] == 10, str(_r4)))
                _r34 = game_data.daily_reward(34, 7)
                check("день 34: Quant-бустер (самый редкий)",
                      lambda: (_r34["reward"] == "quant_token", str(_r34)))
                _r36 = game_data.daily_reward(36, 1)
                check("день 36 заворачивается на 1-й",
                      lambda: (_r36["day"] == 1 and _r36["reward"] == "data", str(_r36)))
                _r7h = game_data.daily_reward(7, 99)
                check("уровень клампится сверху (день 7, ур.99 → 40)",
                      lambda: (_r7h["amount"] == 40, str(_r7h["amount"])))
                check("формат количества: байты/DDT/токен",
                      lambda: (game_data.fmt_daily_amount("data", 524288) == "512 КиБ"
                               and game_data.fmt_daily_amount("ddt", 12) == "12 DDT"
                               and game_data.fmt_daily_amount("cpu_token", 1) == "x1",
                               game_data.fmt_daily_amount("data", 524288)))
                engine._store_daily_state({"currentDay": 4, "LVL": 12,
                                           "lastClaimedDate": 1234567890123.0})
                _ds = db.kv_get("daily_state") or {}
                check("скан сохраняет день/уровень/дату сбора",
                      lambda: (_ds.get("current_day") == 4 and _ds.get("level") == 12
                               and _ds.get("last_claimed") == 1234567890123, str(_ds)))
                engine._store_daily_state({"gameStats": {}})
                check("без полей ежедневки стейт не трогается",
                      lambda: (db.kv_get("daily_state") == _ds,
                               str(db.kv_get("daily_state"))))

                print("selftest: полночь МСК и резервный cron (v2.5.0)")
                from . import webui as webui_mod
                _day_ms = 86400 * 1000
                # 21:00 UTC 01.01.1970 = 00:00 МСК 02.01 — известная граница дня
                _mid = 75600000
                check("полночь МСК: индекс дня растёт ровно по суткам",
                      lambda: (webui_mod.msk_day_index(_mid) == webui_mod.msk_day_index(_mid - 1) + 1
                               and webui_mod.msk_day_index(0) == 0
                               and webui_mod.msk_day_index(3 * 3600 * 1000 - 1) == 0,
                               ""))
                check("полночь МСК: следующая — ровно через сутки",
                      lambda: (webui_mod.next_msk_midnight_ms(_mid) == _mid + _day_ms,
                               str(webui_mod.next_msk_midnight_ms(_mid))))
                check("полночь МСК: за 1 мс до полуночи — ближайшая же",
                      lambda: (webui_mod.next_msk_midnight_ms(_mid - 1) == _mid,
                               str(webui_mod.next_msk_midnight_ms(_mid - 1))))
                _now_iso = _dt.datetime.now().isoformat(timespec="seconds")
                check("резерв: свежий last_cron — не протух",
                      lambda: (webui_mod._fallback_stale_sec(_now_iso) < 60, ""))
                check("резерв: старый last_cron — протух",
                      lambda: (webui_mod._fallback_stale_sec(
                          (_dt.datetime.now() - _dt.timedelta(minutes=20)).isoformat(
                              timespec="seconds")) > webui_mod.CRON_FALLBACK_AFTER_SEC, ""))
                check("резерв: пустой/битый last_cron — None",
                      lambda: (webui_mod._fallback_stale_sec("") is None
                               and webui_mod._fallback_stale_sec("мусор") is None, ""))
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
    sub.add_parser("sell-all", help="собрать всю память: продать все носители сейчас (мимо правил)")
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

    # cron — НЕ блокирующийся: предыдущий проход ещё жив → тихо выходим
    # (иначе зависший проход навсегда останавливает всю автоматизацию);
    # ручные reboot/scan/exchange/sell-all — ждут своей очереди
    wait_lock = args.cmd in ("reboot", "scan", "exchange", "sell-all")
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
            "login": cmd_login, "exchange": cmd_exchange, "sell-all": cmd_sell_all,
        }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
