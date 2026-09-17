#!/bin/bash
# release.sh — универсальный релиз бота в main (GitHub, jadmag007/doomsday-bot).
# Заменил собой одноразовые push_130/push_200/release_251 (удалены при ревизии
# 2026-09-17). Процесс: тест панели в dev-копии → бамп VERSION → клон main →
# синк dev-дерева (вкл. удаления) → selftest → скан утечки PAT → пуш.
#
# Использование:
#   bash /home/z/my-project/scripts/release.sh <версия> "<описание изменений>"
# Пример:
#   bash /home/z/my-project/scripts/release.sh 2.5.2 "фичa X, фикс Y"
set -euo pipefail

VER="${1:?укажи версию, напр. 2.5.2}"
MSG="${2:?укажи описание коммита}"
DEV=/home/z/my-project/doomsday-bot
SCRIPTS=/home/z/my-project/scripts
PAT="$(bash "$SCRIPTS/pat.sh" print)"
[ -n "$PAT" ] || { echo "FATAL: PAT пуст (bash scripts/pat.sh ensure)"; exit 1; }
CLEAN_URL="https://github.com/jadmag007/doomsday-bot.git"
PUSH_URL="https://jadmag007:${PAT}@github.com/jadmag007/doomsday-bot.git"
WORK=/home/z/my-project/.mainwork/release
OLD="$(cat "$DEV/VERSION")"

cd "$DEV"
# бэкап рантайма, который портит тест
for f in config.json state.db logs/bot.log; do
  [ -f "$f" ] && cp "$f" "$f.relbak" || true
done

echo "=== 1. интеграционный тест панели (dev) ==="
bash "$SCRIPTS/test_webui.sh" 2>&1 | tail -6

echo "=== 2. бамп версии $OLD -> $VER ==="
echo "$VER" > VERSION

# восстановить рантайм dev-копии
for f in config.json state.db logs/bot.log; do
  [ -f "$f.relbak" ] && mv "$f.relbak" "$f" || true
done

echo "=== 3. клон main ==="
rm -rf "$WORK"
git clone -q --branch main --single-branch "$PUSH_URL" "$WORK"
git -C "$WORK" remote set-url origin "$CLEAN_URL"
echo "HEAD клона: $(git -C "$WORK" log --oneline -1)"

echo "=== 4. синк dev -> клон (изменения + новые + удаления) ==="
(cd "$WORK" && git ls-files) | sort > /tmp/rel_tracked
(cd "$DEV" && find . -type f \
  ! -path './.git/*' ! -path './logs/*' ! -path './reports/*' ! -path './run/*' \
  ! -path './session/*' ! -path './logsbuf/*' ! -path './__pycache__/*' \
  ! -path './*/__pycache__/*' ! -name '*.db' ! -name '*.db-wal' ! -name '*.db-shm' \
  ! -name 'state.json' ! -name 'worker.lock' ! -name '*.session*' \
  ! -name '*.relbak' ! -name 'config.json' ! -name '*.pyc' \
  | sed 's|^\./||' | sort) > /tmp/rel_dev

CHANGED=0; NEW=0
while read -r p; do
  [ -f "$WORK/$p" ] || { NEW=$((NEW+1)); mkdir -p "$WORK/$(dirname "$p")"; }
  if ! cmp -s "$DEV/$p" "$WORK/$p"; then
    cp "$DEV/$p" "$WORK/$p"; CHANGED=$((CHANGED+1)); echo "  ~ $p"
  fi
done < /tmp/rel_dev
# удаления: трекается в клоне, но в dev больше нет (config.json не трогаем —
# он существует в dev, просто не синкается)
DELETED=0
while read -r p; do
  [ -e "$DEV/$p" ] || { git -C "$WORK" rm -q -- "$p"; DELETED=$((DELETED+1)); echo "  - $p"; }
done < /tmp/rel_tracked
echo "изменено: $CHANGED, новых: $NEW, удалено: $DELETED"

echo "=== 5. selftest в клоне ==="
cd "$WORK"
python3 -m py_compile lib/*.py bin/doomsday 2>/dev/null || python3 -m py_compile lib/*.py
node --check web/app.js && echo "app.js: синтаксис ок"
grep -q "$VER" VERSION && echo "VERSION: $VER ок"

echo "=== 6. скан утечки PAT ==="
if grep -rqF "$PAT" . --exclude-dir=.git; then
  echo "FATAL: строка PAT в релизном дереве — пуш отменён"; exit 1
fi
echo "утечек нет"

echo "=== 7. коммит и пуш ==="
git add -A
if git diff --cached --quiet; then
  echo "ИЗМЕНЕНИЙ НЕТ — на GitHub уже всё актуально"; exit 0
fi
git -c user.name="jadmag007" -c user.email="jadmag007@users.noreply.github.com" \
  commit -qm "v$VER: $MSG"
git push -q "$PUSH_URL" "HEAD:main" 2>/tmp/rel_push_err.txt || {
  echo "ПУШ НЕ УДАЛСЯ:"; cat /tmp/rel_push_err.txt; exit 1; }
echo "ОТПРАВЛЕНО: $(git log --oneline -1)"
echo "Устройству: doomsday start (или git pull + web-restart)."
