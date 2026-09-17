# -*- coding: utf-8 -*-
"""Интеграционный тест lib/starter: git-обновление, pid-файлы, службы, cron-строка.

Сценарий «устройство»: клон v9.8.0 → в origin появляется v9.9.9 →
update_from_git() тянет и запускает (фейковый) install.sh → версия в APP меняется.
Плюс: pid-механика фоновых процессов и полная команда cmd_start/cmd_stop.
"""
import json
import os
import shutil
import subprocess
import sys
import time

BASE = "/home/z/my-project/.starter-test"
ORIGIN = f"{BASE}/origin.git"
SRC = f"{BASE}/src"
APP = f"{BASE}/app"
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
    return r


def git(cwd, *args):
    return sh(["git", "-C", cwd] + list(args))


def seed_repo(version):
    """Наполнить SRC-клон кодом версии version (минимум) и запушить в origin."""
    os.makedirs(f"{SRC}/lib", exist_ok=True)
    with open(f"{SRC}/VERSION", "w") as f:
        f.write(version + "\n")
    # фейковый install.sh: копирует VERSION в APP (как настоящий, но без Termux)
    with open(f"{SRC}/install.sh", "w") as f:
        f.write("#!/bin/sh\nmkdir -p \"$DOOMSDAY_BOT_DIR\"\n"
                "cp -f \"$(dirname \"$0\")/VERSION\" \"$DOOMSDAY_BOT_DIR/VERSION\"\n"
                "echo installed\n")
    os.chmod(f"{SRC}/install.sh", 0o755)
    with open(f"{SRC}/lib/x.py", "w") as f:
        f.write("x = 1\n")
    git(SRC, "add", "-A")
    git(SRC, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-qm", f"v{version}")
    return git(SRC, "push", "-q", "origin", "main")


def setup():
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    sh(["git", "init", "-q", "--bare", "-b", "main", ORIGIN])
    sh(["git", "clone", "-q", ORIGIN, SRC])
    git(SRC, "config", "user.name", "device")
    git(SRC, "config", "user.email", "device@local")
    seed_repo("9.8.0")

    # фейковая установка (APP) с заглушкой bin/doomsday
    os.makedirs(f"{APP}/bin", exist_ok=True)
    os.makedirs(f"{APP}/lib", exist_ok=True)
    os.makedirs(f"{APP}/logs", exist_ok=True)
    open(f"{APP}/lib/__init__.py", "w").close()
    with open(f"{APP}/VERSION", "w") as f:
        f.write("9.8.0\n")
    with open(f"{APP}/bin/doomsday", "w") as f:
        f.write("#!/bin/sh\nsleep 30\n")
    os.chmod(f"{APP}/bin/doomsday", 0o755)
    with open(f"{APP}/config.json", "w") as f:
        json.dump({"telegram": {"api_id": 1, "api_hash": "h", "phone": "+7",
                                "game_bot": "@DoomsDayTyrannybot"},
                   "web": {"host": "127.0.0.1", "port": 8080, "pin": ""}}, f)


def run_py(code):
    env = {**os.environ, "DOOMSDAY_SRC_DIR": SRC, "DOOMSDAY_BOT_DIR": APP,
           "PYTHONPATH": PROJ}
    return sh([PY, "-c", code], cwd=PROJ, env=env)


def main():
    print("setup: клон v9.8.0 + фейковая установка")
    setup()

    print("обновление: в origin появляется v9.9.9")
    seed_repo("9.9.9")
    r = run_py(
        "from lib import starter; "
        "print('updated=', starter.update_from_git()); "
        "print('app_version=', starter._read_file(starter.paths.VERSION_PATH).strip())")
    out = r.stdout or ""
    check("git pull + переустановка", "updated= True" in out, out[-200:] + (r.stderr or "")[-200:])
    check("версия в установке сменилась", "app_version= 9.9.9" in out, out[-100:])

    print("повторный старт: обновлений нет")
    r = run_py("from lib import starter; print('updated=', starter.update_from_git())")
    check("без переустановки", "updated= False" in (r.stdout or ""),
          (r.stdout or "")[-100:])

    print("pid-механика")
    r = run_py(
        "import os\n"
        "from lib import starter\n"
        "print('alive_dead=', starter._pid_alive('999999'))\n"
        "print('alive_self=', starter._pid_alive(os.getpid(), 'python'))\n"
        "starter._write_pid('t', os.getpid())\n"
        "print('readpid=', starter._read_pid('t') == str(os.getpid()))\n"
        "needle = 'не' + 'такого'\n"
        "print('alive_needle=', starter._pid_alive(starter._read_pid('t'), needle))")
    out = r.stdout or ""
    check("мёртвый pid не жив", "alive_dead= False" in out)
    check("живой pid жив", "alive_self= True" in out)
    check("pid-файл читается", "readpid= True" in out)
    check("needle отсекает чужие процессы", "alive_needle= False" in out)

    print("фоновый запуск web через cmd_start (без sv/termux)")
    r = run_py(
        "import argparse, time\n"
        "from lib import starter\n"
        "rc = starter.cmd_start(argparse.Namespace())\n"
        "print('rc=', rc)\n"
        "print('web_pid=', starter._read_pid('web'))\n"
        "print('web_alive=', starter._web_running())\n"
        "starter.services_down()\n"
        "time.sleep(0.3)\n"
        "print('web_alive_after_stop=', starter._web_running())\n")
    out = r.stdout or ""
    check("cmd_start завершается кодом 0", "rc= 0" in out, (r.stderr or "")[-300:])
    check("web запущен с pid-файлом", "web_pid= " in out and "None" not in out.split("web_pid=")[1][:10])
    check("web жив сразу после старта", "web_alive= True" in out)
    check("web остановлен cmd_stop/services_down", "web_alive_after_stop= False" in out)
    check("web.log появился", os.path.isfile(f"{APP}/logs/web.log"))
    # подчистить зависшую заглушку sleep 30
    sh(["pkill", "-f", f"{APP}/bin/doomsday"])

    print("cron_ensure/cron_disable не падают (crontab может отсутствовать)")
    r = run_py("from lib import starter; starter.cron_ensure(); starter.cron_disable(); print('ok=1')")
    check("cron-функции безопасны", "ok=1" in (r.stdout or ""), (r.stderr or "")[-200:])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {FAILS}")
        return 1
    print("Все проверки стартера пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
