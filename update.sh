#!/data/data/com.termux/files/usr/bin/sh
# Ручное обновление из архива в Загрузках: bash ~/doomsday-bot/update.sh [архив.zip] [--force]
set -u
if [ -n "${PREFIX:-}" ]; then export PATH="$PREFIX/bin:$PATH"; fi
APP_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"
PY="$APP_DIR/venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
export DOOMSDAY_BOT_DIR="$APP_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$APP_DIR" || exit 1

ARCHIVE="${1:-}"
FORCE="no"
for a in "$@"; do [ "$a" = "--force" ] && FORCE="yes"; done

if [ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ]; then
    exec "$PY" -m lib.updater --apply "$ARCHIVE" ${FORCE:+--force}
fi
# без аргументов — авто-поиск свежего архива в Загрузках
exec "$PY" -m lib.worker update $([ "$FORCE" = "yes" ] && echo --force)
