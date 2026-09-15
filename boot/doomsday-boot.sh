#!/data/data/com.termux/files/usr/bin/sh
# Загрузочный скрипт для Termux:Boot. Устанавливается в ~/.termux/boot/.
export DOOMSDAY_BOT_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"
if [ -n "${PREFIX:-}" ]; then
    export PATH="$PREFIX/bin:$PATH"
fi

# Держим Termux активным, чтобы cronie успевал проходить по расписанию
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock >/dev/null 2>&1 &

# Поднимаем сервисы (runit стартует их и сам, но на всякий случай)
sleep 2
command -v sv >/dev/null 2>&1 && {
    sv up doomsday-web >/dev/null 2>&1
    sv up cronie >/dev/null 2>&1
}

# Сразу делаем контрольный проход
[ -x "$DOOMSDAY_BOT_DIR/bin/doomsday" ] && "$DOOMSDAY_BOT_DIR/bin/doomsday" cron >/dev/null 2>&1 &
