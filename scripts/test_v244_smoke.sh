#!/bin/bash
# Smoke-тест v2.4.4: «Собрать всю память» — ручная продажа всех байтовых
# носителей мимо правил (регрессия 15.09 вечером: кнопка «Обменять сейчас»
# запускала только скан, потому что склады 79/67/17% не дотягивали до порога
# 90% — пользователю нужна была кнопка «собрать всё немедленно»).
# Проверяем: действие sell-all в API панели, кнопка в разметке, selftest.
set -u
PROJ=/home/z/my-project/doomsday-bot
PY=/home/z/.venv/bin/python3
cd "$PROJ"
FAIL=0
chk() { if [ "$1" = "0" ]; then echo "  ok  $2"; else echo "  FAIL $2"; FAIL=1; fi; }
export DOOMSDAY_BOT_DIR="$PROJ"
export PYTHONPATH="$PROJ"
PORT=18144

cleanup() {
  pkill -f "lib.worker web" 2>/dev/null; sleep 0.5
  pgrep -f "lib.worker web" >/dev/null 2>&1 && pkill -9 -f "lib.worker web"
  rm -f "$PROJ/config.json" /tmp/ddt244-*.json
  rm -rf "$PROJ/logs" "$PROJ/reports" 2>/dev/null
}
trap cleanup EXIT INT TERM

# ---------- 1. конфиг ----------
cat > config.json <<EOF
{"web": {"host": "127.0.0.1", "port": $PORT, "pin": ""},
 "telegram": {"api_id": 1, "api_hash": "x", "game_bot": "@DoomsDayTyrannybot"},
 "schedules": {"scan_interval_minutes": 45}}
EOF

# ---------- 2. кнопки в разметке ----------
grep -q 'data-action="sell-all"' web/index.html
chk $? "кнопка «Собрать всю память» в разметке (2 места)"
n=$(grep -c 'data-action="sell-all"' web/index.html)
[ "$n" = "2" ] ; chk $? "кнопка и на дашборде, и в карточке обмена (найдено $n)"
grep -q 'Обмен по правилам' web/index.html ; chk $? "старая кнопка переименована в «Обмен по правилам»"
grep -q 'sell-all' web/app.js ; chk $? "подтверждение sell-all в app.js"
node --check web/app.js ; chk $? "app.js синтаксически корректен"

# ---------- 3. действие принимается API ----------
$PY -m lib.worker web >/dev/null 2>&1 &
sleep 1.5
code=$(curl -s -o /tmp/ddt244-act.json -w "%{http_code}" \
  -X POST "http://127.0.0.1:$PORT/api/action" -d '{"action":"sell-all"}')
[ "$code" = "200" ] ; chk $? "POST /api/action sell-all → 200 (код $code)"
grep -q '"ok": *true' /tmp/ddt244-act.json ; chk $? "панель подтверждает запуск"
code2=$(curl -s -o /tmp/ddt244-bad.json -w "%{http_code}" \
  -X POST "http://127.0.0.1:$PORT/api/action" -d '{"action":"nope"}')
[ "$code2" = "400" ] ; chk $? "мусорное действие по-прежнему отклоняется (400)"
grep -q 'Собрать всю память' <(curl -s "http://127.0.0.1:$PORT/")
chk $? "кнопка отдаётся сервером панели"

# ---------- 4. CLI-команда ----------
$PY -m lib.worker sell-all --help >/tmp/ddt244-help.txt 2>&1
chk $? "doomsday sell-all — команда существует"

# ---------- 5. selftest с блоком sell-all ----------
out=$($PY -m lib.worker selftest 2>&1)
echo "$out" | grep -q "Все проверки пройдены" ; chk $? "selftest (все проверки)"
n=$(echo "$out" | grep -c "  ok  ")
[ "$n" -ge 95 ] ; chk $? "selftest: не меньше 95 проверок (сейчас $n)"
echo "$out" | grep -q "sell-all" ; chk $? "блок sell-all в selftest"

cleanup
echo
if [ "$FAIL" = "0" ]; then echo "SMOKE v2.4.4: ВСЁ ЗЕЛЁНОЕ ✔"; else echo "SMOKE v2.4.4: ЕСТЬ ПРОВАЛЫ"; exit 1; fi
