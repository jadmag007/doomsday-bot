#!/bin/bash
# Логи устройства: ветка logs приватного репо пользователя.
#
# Конвенция (v2.4.4):
#   logsbuf/bundle-*  — НЕРАЗОБРАННЫЕ выгрузки устройства (разбираем эти)
#   archive/bundle-*  — уже разобранные ассистентом (история, последние 6)
# Устройство после успешного logs-push обрезает отправленные логи ТОЧНО по
# снапшоту (inode+размер), поэтому каждый бандл несёт только записи,
# накопившиеся с момента предыдущей выгрузки — старьё не дублируется.
#
# Использование:
#   fetch_device_logs.sh              — забрать свежий бандл и показать его
#   fetch_device_logs.sh done [имя]   — пометить бандл разобранным:
#                                       logsbuf/<имя> → archive/ + пуш ветки
#                                       (имя = bundle-…; по умолчанию последний)
#
# PAT ищется по цепочке: env GITHUB_PAT → scripts/pat.sh print
#   (pat.sh сам проверяет 3 места: /home/z/my-project/.secrets/gh.pat —
#    ГЛАВНОЕ персистентное, затем зеркала /home/z/.config/... и /tmp/...)
# После клона URL origin сразу очищается от токена; пуш идёт явным URL.
set -euo pipefail

CMD="${1:-fetch}"
case "$CMD" in
    fetch|done) ;;
    *) echo "Использование: fetch_device_logs.sh [fetch|done] [bundle-имя]" >&2; exit 1 ;;
esac

PAT="${GITHUB_PAT:-}"
if [ -z "$PAT" ]; then
    # самолечение + поиск по всем местам (главное — .secrets внутри песочницы)
    bash "$(dirname "$0")/pat.sh" ensure >/dev/null 2>&1 || true
    PAT="$(bash "$(dirname "$0")/pat.sh" print 2>/dev/null || true)"
fi
if [ -z "$PAT" ]; then
    echo "Нужен PAT: env GITHUB_PAT или bash scripts/pat.sh save <token>" >&2
    exit 1
fi
REPO="https://jadmag007:${PAT}@github.com/jadmag007/doomsday-bot.git"
PUSH_URL="https://jadmag007:${PAT}@github.com/jadmag007/doomsday-bot.git"
CLEAN_URL="https://github.com/jadmag007/doomsday-bot.git"
WORK="/home/z/my-project/.logswork"

rm -rf "$WORK"
mkdir -p "$WORK"

echo "=== клон ветки logs ==="
if ! git clone -q --branch logs --single-branch "$REPO" "$WORK/repo" 2>"$WORK/err.txt"; then
    echo "КЛОН НЕ УДАЛСЯ:"; cat "$WORK/err.txt"; exit 1
fi
# сразу убрать токен из конфига клона
git -C "$WORK/repo" remote set-url origin "$CLEAN_URL"

# ------------------------------------------------------------------ done
if [ "$CMD" = "done" ]; then
    BUNDLE="${2:-}"
    cd "$WORK/repo"
    if [ -z "$BUNDLE" ]; then
        BUNDLE="$(ls -1 logsbuf 2>/dev/null | grep '^bundle-' | sort | tail -1 || true)"
    fi
    if [ -z "$BUNDLE" ]; then
        echo "Неразобранных бандлов нет — архивировать нечего."
        exit 0
    fi
    if [ ! -d "logsbuf/$BUNDLE" ]; then
        echo "Бандл logsbuf/$BUNDLE не найден в ветке logs." >&2
        exit 1
    fi
    echo "=== архивирую: logsbuf/$BUNDLE → archive/ ==="
    mkdir -p archive
    git mv "logsbuf/$BUNDLE" "archive/$BUNDLE"
    # прун архива: храним последние 6 бандлов
    ls -1d archive/bundle-* 2>/dev/null | sort | head -n -6 | while read -r d; do
        git rm -rq "$d"
    done
    git -c user.name="assistant" -c user.email="assistant@sandbox.local" \
        commit -qm "archive: $BUNDLE разобран (в logsbuf/ — только неразобранное)"
    git push -q "$PUSH_URL" "HEAD:logs"
    echo "✓ запушено: $BUNDLE → archive/"
    REMAIN="$(ls -1 logsbuf 2>/dev/null | grep -c '^bundle-' || true)"
    echo "осталось неразобранных: ${REMAIN:-0}"
    exit 0
fi

# ------------------------------------------------------------------ fetch
echo
echo "=== история ветки logs ==="
git -C "$WORK/repo" log --oneline -10

echo
echo "=== неразобранные бандлы (logsbuf/) ==="
FRESH="$(ls -1 "$WORK/repo/logsbuf" 2>/dev/null | grep '^bundle-' | sort | tail -1 || true)"
ARCHIVED="$(ls -1 "$WORK/repo/archive" 2>/dev/null | grep -c '^bundle-' || true)"
INQUEUE="$(ls -1 "$WORK/repo/logsbuf" 2>/dev/null | grep -c '^bundle-' || true)"
echo "в очереди: ${INQUEUE:-0} | в архиве: ${ARCHIVED:-0}"
if [ -z "$FRESH" ]; then
    echo "(неразобранных бандлов нет — всё разобрано, ждём новой выгрузки с устройства)"
    exit 0
fi
BUNDLE_DIR="logsbuf/$FRESH"
echo "разбираем: $BUNDLE_DIR"

echo
echo "=== meta.json ==="
cat "$WORK/repo/$BUNDLE_DIR/meta.json" 2>/dev/null || echo "(нет meta.json)"

echo
echo "=== состав бандла ==="
ls -la "$WORK/repo/$BUNDLE_DIR/logs" "$WORK/repo/$BUNDLE_DIR/reports" 2>/dev/null | head -30

echo
echo "=== хвосты логов (bot/cron/run-*) ==="
for f in "$WORK/repo/$BUNDLE_DIR/logs"/*.log*; do
    [ -f "$f" ] || continue
    echo "--- $f (последние 60 строк) ---"
    tail -60 "$f"
done

echo
echo "=== поиск ошибок/трейсбеков в бандле ==="
grep -rn -i -E "traceback|error|exception|failed|критич" "$WORK/repo/$BUNDLE_DIR" \
     --include='*.log' --include='*.json' | head -40 || echo "(чисто)"

echo
echo "=== ГОТОВО: разбирай $WORK/repo/$BUNDLE_DIR ==="
echo "после разбора ЗАКРОЙ вопрос одной командой:"
echo "  bash scripts/fetch_device_logs.sh done          # архивирует этот бандл"
echo "  bash scripts/fetch_device_logs.sh done $FRESH   # или явно по имени"
