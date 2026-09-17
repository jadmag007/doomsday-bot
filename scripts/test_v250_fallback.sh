#!/bin/bash
# Функциональный тест резервного cron-планировщика: панель с протухшим
# last_cron_ts должна сама запустить `doomsday cron` в течение ~70 сек.
set -u
PROJ=/home/z/my-project/doomsday-bot
PY=/home/z/.venv/bin/python3
cd "$PROJ"
export DOOMSDAY_BOT_DIR="$PROJ"
export PYTHONPATH="$PROJ"
PORT=18156

cleanup() {
  pkill -f "lib.worker web" 2>/dev/null; sleep 0.5
  pgrep -f "lib.worker web" >/dev/null 2>&1 && pkill -9 -f "lib.worker web"
  rm -f "$PROJ/config.json"
  rm -rf "$PROJ/logs" "$PROJ/reports" 2>/dev/null
}
trap cleanup EXIT INT TERM

cat > config.json <<EOF
{"web": {"host": "127.0.0.1", "port": $PORT, "pin": ""},
 "telegram": {"api_id": 1, "api_hash": "x", "game_bot": "@DoomsDayTyrannybot"},
 "schedules": {"scan_interval_minutes": 45}}
EOF

# протухший last_cron_ts: 40 минут назад
$PY - <<'EOF'
import sys, datetime
sys.path.insert(0, ".")
from lib import db
old = (datetime.datetime.now() - datetime.timedelta(minutes=40)).isoformat(timespec="seconds")
db.kv_set("last_cron_ts", old)
print("staged: last_cron_ts =", old)
EOF

$PY -m lib.worker web > /tmp/ddt250-fb.log 2>&1 &
sleep 2
before=$($PY -c "
import sys; sys.path.insert(0, '.')
from lib import db
print(db.kv_get('last_cron_ts'))")
echo "last_cron_ts до: $before"

# ждём до 80 сек: цикл проверки спит 60с, затем должен заспавнить проход
for i in $(seq 1 16); do
  sleep 5
  now=$($PY -c "
import sys; sys.path.insert(0, '.')
from lib import db
print(db.kv_get('last_cron_ts'))")
  if [ "$now" != "$before" ]; then
    echo "last_cron_ts обновился (через ~$((i*5+2)) сек): $now"
    $PY -c "
import sys; sys.path.insert(0, '.')
from lib import db
evs = [e for e in db.events_list(limit=10) if 'Резервный' in (e.get('title') or '')]
print('событие резервного прохода:', evs[0]['title'] if evs else 'НЕТ')
assert evs, 'нет события о резервном проходе'
"
    echo "FALLBACK TEST: OK"
    exit 0
  fi
done
echo "FALLBACK TEST: FAIL — last_cron_ts не обновился за 80 сек"
exit 1
