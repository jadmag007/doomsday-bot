#!/usr/bin/env bash
# Интеграционный тест веб-панели: сервер в фоне + curl по всем эндпоинтам
set -u
cd /home/z/my-project/doomsday-bot
rm -f state.db config.json logs/bot.log
python3 - <<'EOF'
import json
from lib import config as cfgmod
cfg = cfgmod.DEFAULTS
cfg["telegram"]["api_id"] = 12345
cfg["telegram"]["api_hash"] = "testhash1234567890"
cfg["telegram"]["phone"] = "+79990000000"
cfg["web"]["port"] = 18099
cfgmod.save(cfg)
from lib import db
db.kv_set("last_reboot_ts", "2026-09-15T10:00:00")
db.kv_set("last_scan_ts", "2026-09-15T11:00:00")
db.resource_snapshot([
    {"name": "Дерево", "current": 4800, "max": 5000, "state": ""},
    {"name": "Нефть", "current": 10000, "max": 10000, "state": "Склад переполнен"},
])
db.event("reboot", "Тест: ребут", "шаги ок")
db.event("chat", "Склад переполнен: Нефть", "пора разгрузить", severity="warn")
print("подготовка данных done")
EOF

python3 -m lib.worker web > /tmp/webui_test.log 2>&1 &
WEBPID=$!
sleep 1.5

echo "== GET / (index.html) =="
curl -s -o /tmp/idx.html -w "%{http_code} %{size_download}b\n" http://127.0.0.1:18099/
rg -c 'id="tab-dash"' /tmp/idx.html && echo "  разметка дашборда найдена"
echo "== GET /style.css /app.js =="
curl -s -o /dev/null -w "style: %{http_code} %{size_download}b\n" http://127.0.0.1:18099/style.css
curl -s -o /dev/null -w "js:    %{http_code} %{size_download}b\n" http://127.0.0.1:18099/app.js
echo "== GET /api/overview =="
curl -s http://127.0.0.1:18099/api/overview | python3 -c "
import json,sys
o = json.load(sys.stdin)
t = o['timers']
print('  version:', o['version'])
print('  farm_cycle:', t['farm_cycle'])
print('  scan_in_sec:', t['scan_in_sec'], '| next_scan:', t['next_scan'])
print('  daily:', o['daily'])
assert 'daily' in o and 'reset_in_sec' in o['daily'], 'нет секции daily'
assert 0 <= o['daily']['reset_in_sec'] < 86400, 'daily.reset_in_sec вне суток'
print('  resources:', [(r['name'], r['current'], r['max'], r['pct'], r['state']) for r in o['resources']])
print('  events:', len(o['events']), '| tma_configured:', o['tma_configured'])
print('  OK: daily-секция в порядке')
"
echo "== GET /api/config (маска api_hash) =="
curl -s http://127.0.0.1:18099/api/config | python3 -c "
import json,sys
c = json.load(sys.stdin)
print('  api_hash:', c['telegram']['api_hash'], '| _has_api_hash:', c['_has_api_hash'])
"
echo "== PUT /api/config (смена таймеров) =="
curl -s -X PUT -H "Content-Type: application/json" http://127.0.0.1:18099/api/config \
  -d "$(python3 -c "
import json, sys
sys.path.insert(0, '.')
from lib import config as cfgmod
c = cfgmod.DEFAULTS
c['telegram']['api_id'] = 12345
c['telegram']['api_hash'] = 'testhash1234567890'
c['web']['port'] = 18099
c['schedules']['reboot_interval_hours'] = 12
c['schedules']['scan_interval_minutes'] = 30
print(json.dumps(c))")" | python3 -c "
import json,sys
r = json.load(sys.stdin)
print('  ok:', r.get('ok'), '| scan_interval:', r.get('config',{}).get('schedules',{}).get('scan_interval_minutes'))
"
echo "== PUT с невалидным конфигом =="
curl -s -X PUT -H "Content-Type: application/json" http://127.0.0.1:18099/api/config \
  -d '{"telegram": {"api_id": 0, "api_hash": ""}}' | head -c 200; echo
echo "== GET /api/events =="
curl -s "http://127.0.0.1:18099/api/events?limit=3" | python3 -c "import json,sys; print('  событий:', len(json.load(sys.stdin)))"
echo "== GET /api/log =="
curl -s "http://127.0.0.1:18099/api/log?lines=10" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  строк лога:', len(d['lines']))"
echo "== POST /api/action (scan — ожидаем graceful провал без Telegram) =="
curl -s -X POST -H "Content-Type: application/json" http://127.0.0.1:18099/api/action -d '{"action":"test-notify"}' | head -c 200; echo
echo "== 404 на неизвестный эндпоинт =="
curl -s -o /dev/null -w "  status: %{http_code}\n" http://127.0.0.1:18099/api/nope
echo "== traversal защита =="
curl -s -o /dev/null -w "  status: %{http_code}\n" --path-as-is "http://127.0.0.1:18099/../../etc/passwd"

kill $WEBPID 2>/dev/null
echo "== логи сервера =="
tail -5 /tmp/webui_test.log
