#!/bin/bash
# Smoke-тест v2.5.1 (диагностика 16.09 20:44→20:54: ребут опоздал на 10 мин):
#   1. «Сервер производства» — игровой блок (Осталось/Статус 200/503, полоса);
#      время ребута из статусбара УБРАНО (просьба 16.09) — только в карточке.
#   2. Точный дожим конца цикла: _cycle_end_watch_due — панель не ждёт
#      11-минутную сетку резервного планировщика.
#   3. Истёкший цикл: троттлинг ошибок ребута 2 мин вместо 20.
#   4. Резервные проходы пишут план в cron.log с меткой [fallback].
set -u
PROJ=/home/z/my-project/doomsday-bot
PY=/home/z/.venv/bin/python3
cd "$PROJ"
FAIL=0
chk() { if [ "$1" = "0" ]; then echo "  ok  $2"; else echo "  FAIL $2"; FAIL=1; fi; }
export DOOMSDAY_BOT_DIR="$PROJ"
export PYTHONPATH="$PROJ"

# ---------- 1. разметка: игровой блок производства, без чипа ребута ----------
grep -q 'id="farm-card"' web/index.html ; chk $? "карточка «Сервер производства» в разметке"
grep -q 'id="t-farm-status"' web/index.html ; chk $? "строка «Статус: …» в карточке"
grep -q 'id="t-farm-bar"' web/index.html ; chk $? "полоса цикла (farm-bar) в карточке"
! grep -q 'id="sb-reboot"' web/index.html ; chk $? "чип ребута УБРАН из статусбара"
grep -q 'id="sb-ddt"' web/index.html ; chk $? "DDT-чип (иконка + «+N time») на месте"
grep -q 'id="sb-daily"' web/index.html ; chk $? "чип ежедневного бонуса на месте"
grep -q 'farm-card.done' web/style.css ; chk $? "красное состояние карточки (done)"
grep -q 'Статус: 200' web/app.js ; chk $? "игровой статус 200 в app.js"
grep -q 'Статус: 503' web/app.js ; chk $? "игровой статус 503 в app.js"
grep -q 'Завершено!' web/app.js ; chk $? "текст «Завершено!» как в игре"
node --check web/app.js ; chk $? "app.js синтаксически корректен"

# ---------- 2. воркер: истёкший цикл → ретраи каждые 2 мин ----------
$PY - <<'EOF'
import sys, datetime, time
sys.path.insert(0, ".")
from lib import worker, config as cfgmod, db
cfg = cfgmod._deep_merge(cfgmod.DEFAULTS, {})
now = datetime.datetime.now()
ends_ms = lambda sec: str(int((time.time() + sec) * 1000))
# цикл истёк 30 с назад, фейл ребута 3 мин назад — ретрай должен быть разрешён
db.kv_set("passive_farm_ends_at", ends_ms(-30))
db.kv_set("cycle_reboot_guard", None)
db.kv_set("last_fail_reboot",
          (now - datetime.timedelta(minutes=3)).isoformat(timespec="seconds"))
assert worker._reboot_due_cycle(cfg, datetime.datetime.now()) is True, \
    "истёкший цикл + фейл 3 мин назад: ретрай заблокирован (должен быть 2-мин троттлинг)"
# фейл только что — даже при истёкшем цикле держим паузу 2 мин
db.kv_set("last_fail_reboot", db.now_iso())
assert worker._reboot_due_cycle(cfg, datetime.datetime.now()) is False, \
    "фейл только что: троттлинг не работает"
db.kv_set("last_fail_reboot", None)
db.kv_set("passive_farm_ends_at", None)
print("worker: истёкший цикл — 2-минутный троттлинг ошибок работает")
EOF
chk $? "воркер: ретраи ребута при истёкшем цикле — 2 мин, не 20"

# ---------- 3. панель: точный дожим конца цикла ----------
$PY - <<'EOF'
import sys, datetime, time
sys.path.insert(0, ".")
from lib import webui, db
now = datetime.datetime.now()
ends_ms = lambda sec: str(int((time.time() + sec) * 1000))
# далеко до конца — не дёргаемся
db.kv_set("passive_farm_ends_at", ends_ms(2 * 3600))
assert webui._cycle_end_watch_due(now) is False, "далеко до конца: не должно быть дожима"
# почти конец (< 2 мин) — дожим
db.kv_set("passive_farm_ends_at", ends_ms(60))
assert webui._cycle_end_watch_due(now) is True, "почти конец: нужен дожим"
# истёк 1 мин назад, ребута не было — дожим
db.kv_set("passive_farm_ends_at", ends_ms(-60))
db.kv_set("last_reboot_ts",
          (now - datetime.timedelta(hours=12)).isoformat(timespec="seconds"))
assert webui._cycle_end_watch_due(now) is True, "истёк и не ребутнут: нужен дожим"
# истёк, но ребут уже сделан (после конца) — спокойно
db.kv_set("last_reboot_ts", db.now_iso())
assert webui._cycle_end_watch_due(now) is False, "ребут после конца сделан: дожим не нужен"
# истёк 40 мин назад (аварийное окно 30 мин закрыто) — обычная сетка
db.kv_set("passive_farm_ends_at", ends_ms(-2400))
db.kv_set("last_reboot_ts",
          (now - datetime.timedelta(hours=12)).isoformat(timespec="seconds"))
assert webui._cycle_end_watch_due(now) is False, "конец >30 мин назад: авральный режим выключен"
db.kv_set("passive_farm_ends_at", None)
db.kv_set("last_reboot_ts", None)
print("webui: дожим конца цикла — все ветки работают")
EOF
chk $? "панель: _cycle_end_watch_due (дожим конца цикла)"

# ---------- 4. резервный проход логируется и размечен ----------
grep -q 'DOOMSDAY_CRON_FALLBACK' lib/worker.py ; chk $? "метка [fallback] в плане (воркер)"
grep -q 'DOOMSDAY_CRON_FALLBACK' lib/webui.py ; chk $? "панель передаёт метку + пишет в cron.log"

# ---------- 5. selftest воркера целиком (включая новые проверки) ----------
$PY -m lib.worker selftest >/tmp/ddt251-selftest.log 2>&1
chk $? "selftest воркера (полный, с новыми проверками троттлинга)"
grep -q "истёкший цикл: фейл 3 мин назад" /tmp/ddt251-selftest.log \
  ; chk $? "selftest: новая проверка «ретрай через 2 мин» выполняется"

echo
if [ "$FAIL" = "0" ]; then echo "=== v2.5.1 smoke: ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ==="; else echo "=== v2.5.1 smoke: ЕСТЬ СБОИ ==="; exit 1; fi
