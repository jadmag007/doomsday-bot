#!/data/data/com.termux/files/usr/bin/sh
# ============================================================
#  doomsday-bot — стартёр (тонкая обёртка).
#
#  Вся логика — в lib/starter.py, поэтому стартёр обновляется
#  вместе с кодом и не может разойтись с ним по версии.
#
#  Запуск вручную:      doomsday start
#  Или напрямую:        sh ~/doomsday-bot/start.sh
#  После перезагрузки:  автоматически (Termux:Boot, см. README)
# ============================================================
set -u

[ -n "${PREFIX:-}" ] && export PATH="$PREFIX/bin:$PATH"

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
BIN="$DIR/bin/doomsday"
[ -x "$BIN" ] || BIN="${HOME:-}/doomsday-bot/bin/doomsday"

if [ -x "$BIN" ]; then
    exec "$BIN" start
else
    echo "Не найден bin/doomsday ($BIN)." >&2
    echo "Переустановите: bash ~/doomsday-src/install.sh" >&2
    exit 1
fi
