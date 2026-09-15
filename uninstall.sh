#!/data/data/com.termux/files/usr/bin/sh
# Полное удаление Doomsday Tyranny Bot (кроме venv можно оставить всё)
set -u
if [ -n "${PREFIX:-}" ]; then export PATH="$PREFIX/bin:$PATH"; fi
APP_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"

read -p "Удалить $APP_DIR целиком (конфиг, сессию, журнал)? [y/N] " ANS
case "$ANS" in
    y|Y) ;;
    *) echo "Отменено"; exit 0 ;;
esac

# остановить сервис
command -v sv >/dev/null 2>&1 && sv down doomsday-web >/dev/null 2>&1
rm -rf "$PREFIX/var/service/doomsday-web" 2>/dev/null

# убрать cron
if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v '# doomsday-bot' | crontab - 2>/dev/null
fi

# убрать boot
rm -f "$HOME/.termux/boot/90-doomsday-boot.sh"

rm -rf "$APP_DIR"
echo "Удалено. Данные Telegram-сессии стёрты вместе с каталогом."
