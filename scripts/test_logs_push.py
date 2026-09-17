# -*- coding: utf-8 -*-
"""Интеграционный тест lib/logspush: буфер логов → ветка logs (bare-репозиторий как GitHub).

Проверяет:
  1. первый push создаёт ветку logs с бандлом (мета+логи+отчёты+конфиг);
  2. повторный push добавляет коммит поверх (история растёт);
  3. рабочий каталог main НЕ затронут (никаких лишних файлов в git status);
  4. локальный logsbuf/ очищен после успешного push;
  5. parse ресурсов/пресет — см. selftest.
"""
import json
import os
import shutil
import subprocess
import sys

BASE = "/home/z/my-project/.logspush-test"
ORIGIN = f"{BASE}/origin.git"     # «GitHub»
SRC = f"{BASE}/src"               # ~/doomsday-src на устройстве
APP = f"{BASE}/app"               # ~/doomsday-bot на устройстве
PROJ = "/home/z/my-project/doomsday-bot"

PY = sys.executable
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def sh(cmd, cwd=None, env=None, inp=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, input=inp, capture_output=True,
                       text=True, errors="replace")
    if r.returncode != 0:
        print("    !", " ".join(cmd), "→", r.returncode, (r.stderr or "")[:300])
    return r


def git(cwd, *args, env=None, inp=None):
    return sh(["git", "-C", cwd] + list(args), env=env, inp=inp)


def setup():
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    # «GitHub»: bare с одним коммитом-заглушкой в main
    sh(["git", "init", "-q", "--bare", "-b", "main", ORIGIN])
    tmp = f"{BASE}/seed"
    sh(["git", "clone", "-q", ORIGIN, tmp])
    open(f"{tmp}/README.md", "w").write("test repo\n")
    git(tmp, "add", "-A")
    git(tmp, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    git(tmp, "push", "-q", "origin", "main")
    # клон-«исходники» на устройстве
    sh(["git", "clone", "-q", ORIGIN, SRC])
    git(SRC, "config", "user.name", "device")
    git(SRC, "config", "user.email", "device@local")
    # фейковая установка с логами/отчётами/конфигом
    os.makedirs(f"{APP}/logs", exist_ok=True)
    os.makedirs(f"{APP}/reports", exist_ok=True)
    os.makedirs(f"{APP}/session", exist_ok=True)
    with open(f"{APP}/VERSION", "w") as f:
        f.write("9.9.9\n")
    with open(f"{APP}/logs/cron.log", "w") as f:
        f.write("[cron] строка 1\n[cron] строка 2\n")
    with open(f"{APP}/logs/bot.log", "w") as f:
        f.write("INFO test\n" * 10)
    with open(f"{APP}/reports/discovery-20260915.json", "w") as f:
        json.dump({"webview_url": "x", "game_bot": "@DoomsDayTyrannybot"}, f)
    with open(f"{APP}/config.json", "w") as f:
        json.dump({"telegram": {"api_id": 1, "api_hash": "h", "phone": "+7", "game_bot": "@DoomsDayTyrannybot"}}, f)
    with open(f"{SRC}/config.json", "w") as f:
        f.write("same config\n")
    git(SRC, "add", "config.json")
    git(SRC, "commit", "-qm", "config в репо (как у пользователя)")


def run_push():
    env = {**os.environ, "DOOMSDAY_SRC_DIR": SRC, "DOOMSDAY_BOT_DIR": APP,
           "PYTHONPATH": PROJ}
    r = sh([PY, "-c",
            "import sys; from lib import logspush; "
            "import json; print(json.dumps(logspush.push_logs(message='тестовый бандл')))"],
           cwd=PROJ, env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": r.stdout[-300:] + r.stderr[-300:]}


def main():
    print("setup: bare-репозиторий + клон + фейковые логи")
    setup()

    print("push 1")
    res1 = run_push()
    check("push успешен", res1.get("ok"), str(res1.get("error", ""))[:200])
    check("коммит создан", bool(res1.get("commit")))

    # ветка logs на «GitHub» существует и содержит файлы бандла
    ls = sh(["git", "--git-dir", ORIGIN, "ls-tree", "-r", "--name-only", "logs"])
    files = (ls.stdout or "").splitlines()
    check("ветка logs на origin", bool(files), f"{len(files)} файлов")
    check("метаданные в бандле", any(f.endswith("meta.json") for f in files))
    check("лог в бандле", any(f.endswith("logs/cron.log") for f in files))
    check("отчёт в бандле", any("discovery-20260915" in f for f in files))
    check("конфиг в бандле", any(f.endswith("config-app.json") for f in files))
    check("logsbuf не остался на диске", not os.path.isdir(f"{SRC}/logsbuf")
          or not os.listdir(f"{SRC}/logsbuf"))

    # meta.json содержательный
    cat = sh(["git", "--git-dir", ORIGIN, "show", "logs:logsbuf/bundle-"
              + sorted(f.split("/")[1].replace("bundle-", "").split("/")[0]
                       for f in files if f.startswith("logsbuf/"))[0] + "/meta.json"])
    try:
        meta = json.loads(cat.stdout)
        check("meta: версия и head", meta.get("version") == "9.9.9"
              and bool(meta.get("src_head")), str(meta.get("version")))
    except ValueError:
        check("meta: версия и head", False, "meta.json не читается")

    # рабочий каталог main не затронут
    st = git(SRC, "status", "--porcelain")
    check("рабочий каталог чист", (st.stdout or "").strip() == "", (st.stdout or "")[:200])

    print("push 2 (поверх истории + ТОЧНАЯ очистка отправленного, v2.4.4)")
    with open(f"{APP}/logs/cron.log", "a") as f:
        f.write("[cron] строка 3\n")
    # специально НЕ состариваем файлы: свежесть по mtime больше не помеха —
    # обрезка идёт по снапшоту (inode+размер) из манифеста бандла.
    # Сценарий бага 15.09: cron.log менялся минуту назад (свежий скан),
    # но после пуша должен остаться пустым, а не уехать целиком в бандл 3
    res2 = run_push()
    check("второй push успешен", res2.get("ok"), str(res2.get("error", ""))[:200])
    check("свежий по mtime лог обрезан сразу (без 5-мин окна)",
          os.path.getsize(f"{APP}/logs/cron.log") == 0,
          f"{os.path.getsize(f'{APP}/logs/cron.log')} байт")
    check("bot.log тоже обрезан", os.path.getsize(f"{APP}/logs/bot.log") == 0,
          f"{os.path.getsize(f'{APP}/logs/bot.log')} байт")
    check("отправленный отчёт удалён",
          not os.path.isfile(f"{APP}/reports/discovery-20260915.json"))
    check("результат сообщает число очищенных", isinstance(res2.get("logs_cleared"), int),
          str(res2.get("logs_cleared")))
    log = sh(["git", "--git-dir", ORIGIN, "log", "--format=%s", "logs"])
    msgs = (log.stdout or "").strip().splitlines()
    check("история ветки растёт", len(msgs) == 2 and msgs[0] == "тестовый бандл", str(msgs))
    cat2 = sh(["git", "--git-dir", ORIGIN, "show", "logs:logsbuf/"
               + [f for f in (sh(["git", "--git-dir", ORIGIN, "ls-tree", "-r",
                                  "--name-only", "logs"]).stdout or "").splitlines()
                  if f.endswith("cron.log")][0].split("/")[1] + "/logs/cron.log"])
    check("второй бандл свежее", "строка 3" in (cat2.stdout or ""))

    print("push 3: дозапись ПОСЛЕ сборки не теряется (гонка писатель/пуш)")
    # эмулируем: между сборкой бандла и успешным пушем в лог дописали строку —
    # снапшот уже зафиксировал размер ДО дописи, значит обрезка обязана сохранить новую строку
    env = {**os.environ, "DOOMSDAY_SRC_DIR": SRC, "DOOMSDAY_BOT_DIR": APP,
           "PYTHONPATH": PROJ}
    with open(f"{APP}/logs/cron.log", "a") as f:
        f.write("[cron] до сборки\n")
    r = sh([PY, "-c",
            "import os, json\n"
            "from lib import logspush\n"
            "orig = logspush.build_bundle\n"
            "def wrapped(d):\n"
            "    m = orig(d)\n"
            "    # допись ПОСЛЕ сборки (снапшоты уже сняты) — имитация активного писателя\n"
            "    with open(os.path.join(logspush.paths.LOG_DIR, 'cron.log'), 'a') as f:\n"
            "        f.write('[cron] после сборки\\n')\n"
            "    return m\n"
            "logspush.build_bundle = wrapped\n"
            "logspush.SRC_DIR = os.environ['DOOMSDAY_SRC_DIR']\n"
            "print(json.dumps(logspush.push_logs(message='гонка')))",
            ], cwd=PROJ, env=env)
    try:
        res3 = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        res3 = {"ok": False, "error": (r.stdout or "")[-200:] + (r.stderr or "")[-200:]}
    check("push с гонкой успешен", res3.get("ok"), str(res3.get("error", ""))[:200])
    _cron = open(f"{APP}/logs/cron.log").read()
    check("дописанное после сборки осталось на устройстве",
          _cron == "[cron] после сборки\n", repr(_cron))

    print("push 4: нет сети до origin (ошибка обрабатывается, бандл сохраняется)")
    # порт, на котором точно ничего нет
    broken = dict(os.environ)
    res4_env = {**broken, "DOOMSDAY_SRC_DIR": SRC, "DOOMSDAY_BOT_DIR": APP}
    r = sh([PY, "-c",
            "import os; from lib import logspush; "
            "logspush.SRC_DIR = os.environ['DOOMSDAY_SRC_DIR']; "
            "import json; "
            # подменяем origin на недоступный
 "import subprocess as sp; sp.run(['git','-C',logspush.SRC_DIR,'remote','set-url','origin','http://127.0.0.1:1/x.git'],capture_output=True); "
            "print(json.dumps(logspush.push_logs()))"],
           cwd=PROJ, env=res4_env)
    try:
        res4 = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res4 = {"ok": False, "error": "parse " + r.stdout[-200:]}
    check("ошибка сети не роняет команду", res4.get("ok") is False and bool(res4.get("error")))
    check("бандл сохранён при провале", os.path.isdir(f"{SRC}/logsbuf")
          and len(os.listdir(f"{SRC}/logsbuf")) >= 1)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {FAILS}")
        return 1
    print("Все проверки logspush пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
