#!/data/data/com.termux/files/usr/bin/sh
# Автозапуск после перезагрузки телефона (Termux:Boot).
# Устанавливается в ~/.termux/boot/90-doomsday-boot.sh
# Полная логика (обновление из git + сервисы + проход) — в start.sh.
export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export DOOMSDAY_BOT_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"
exec sh "$DOOMSDAY_BOT_DIR/start.sh"
