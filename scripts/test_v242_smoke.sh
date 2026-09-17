#!/bin/bash
# Smoke-тест v2.4.2: обновление панели сразу после ребута (регрессия 15.09
# 19:15: ребут прошёл, но панель висела с «требуется ребут», а кассета на
# 90% не обменялась — скана после ребута не было 45+ минут).
# Проверяем: контрольный скан в _due и в таймерах панели, selftest, интеграционный тест.
set -u
PROJ=/home/z/my-project/doomsday-bot
PY=/home/z/.venv/bin/python3
cd "$PROJ"
FAIL=0
chk() { if [ "$1" = "0" ]; then echo "  ok  $2"; else echo "  FAIL $2"; FAIL=1; fi; }
export DOOMSDAY_BOT_DIR="$PROJ"
export PYTHONPATH="$PROJ"
PORT=18133

cleanup() {
  pkill -f "lib.worker web" 2>/dev/null; sleep 0.5
  pgrep -f "lib.worker web" >/dev/null 2>&1 && pkill -9 -f "lib.worker web"
  rm -f "$PROJ/config.json" /tmp/ddt242-*.json
  rm -rf "$PROJ/logs" "$PROJ/reports" 2>/dev/null
}
trap cleanup EXIT INT TERM

# ---------- 1. конфиг ----------
cat > config.json <<EOF
{"web": {"host": "127.0.0.1", "port": $PORT, "pin": ""},
 "telegram": {"api_id": 1, "api_hash": "x", "game_bot": "@DoomsDayTyrannybot"},
 "schedules": {"scan_interval_minutes": 45}}
EOF

# ---------- 2. контрольный скан в таймерах панели ----------
$PY - <<'EOF'
import sys, time, json, datetime
sys.path.insert(0, "/home/z/my-project/doomsday-bot")
from lib import db
db.kv_set("passive_farm_ends_at", str(int((time.time() + 3 * 3600) * 1000)))
db.kv_set("last_scan_ts", datetime.datetime.now().isoformat(timespec="seconds"))
force = datetime.datetime.now() + datetime.timedelta(minutes=5)
db.kv_set("force_scan_at", force.isoformat(timespec="seconds"))
EOF
$PY -m lib.worker web >/dev/null 2>&1 &
sleep 1.5
curl -s "http://127.0.0.1:$PORT/api/overview" -o /tmp/ddt242-ov.json
$PY - <<'EOF'
import json, sys, datetime
ov = json.load(open("/tmp/ddt242-ov.json"))
t = ov.get("timers") or {}
force_raw = t.get("next_scan")
ok1 = bool(force_raw)
if ok1:
    diff = (datetime.datetime.fromisoformat(force_raw)
            - datetime.datetime.now()).total_seconds()
    # регулярный скан был бы через ~45 мин, контрольный — через ~5
    ok1 = 120 <= diff <= 330
print("FORCE_SCAN_SHOWN" if ok1 else f"BAD next_scan={force_raw}")
sys.exit(0 if ok1 else 1)
EOF
chk $? "контрольный скан отображается как ближайший (а не интервал 45 мин)"

# ---------- 3. selftest с новыми проверками ----------
out=$($PY -m lib.worker selftest 2>&1)
echo "$out" | grep -q "Все проверки пройдены" ; chk $? "selftest (все проверки)"
n=$(echo "$out" | grep -c "  ok  ")
[ "$n" -ge 85 ] ; chk $? "selftest: не меньше 85 проверок (сейчас $n)"
echo "$out" | grep -q "обновление панели после ребута" ; chk $? "блок регрессии 19:15 в selftest"

# ---------- 4. интеграционный тест ребута (мок-сервер игры) ----------
$PY /home/z/my-project/scripts/test_reboot_session.py >/tmp/ddt242-reboot.log 2>&1
chk $? "интеграционный тест ребута (A-E, мок Firebase)"
grep -q "sellItem вызван для кассеты сразу после ребута" /tmp/ddt242-reboot.log
chk $? "автообмен сразу после ребута (тест E)"

cleanup
echo
if [ "$FAIL" = "0" ]; then echo "SMOKE v2.4.2: ВСЁ ЗЕЛЁНОЕ ✔"; else echo "SMOKE v2.4.2: ЕСТЬ ПРОВАЛЫ"; exit 1; fi
