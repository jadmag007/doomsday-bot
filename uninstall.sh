#!/data/data/com.termux/files/usr/bin/sh
# Полное удаление Doomsday Tyranny Bot
set -u
if [ -n "${PREFIX:-}" ]; then export PATH="$PREFIX/bin:$PATH"; fi
APP_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"
SRC_DIR="${DOOMSDAY_SRC_DIR:-$HOME/doomsday-src}"

read -p "Удалить $APP_DIR целиком (конфиг, сессию, журнал)? [y/N] " ANS
case "$ANS" in
    y|Y) ;;
    *) echo "Отменено"; exit 0 ;;
esac

# остановить панель и службы
command -v sv >/dev/null 2>&1 && sv down doomsday-web >/dev/null 2>&1
[ -f "$APP_DIR/bin/doomsday" ] && "$APP_DIR/bin/doomsday" stop >/dev/null 2>&1
rm -rf "$PREFIX/var/service/doomsday-web" 2>/dev/null

# убрать cron
if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v '# doomsday-bot' | crontab - 2>/dev/null
fi

# убрать глобальную команду и boot
rm -f "$PREFIX/bin/doomsday" "$HOME/.termux/boot/90-doomsday-boot.sh"

rm -rf "$APP_DIR"
echo "Удалено. Данные Telegram-сессии стёрты вместе с каталогом."
echo "Каталог исходников $SRC_DIR (git-клон) оставлен — удалите вручную, если не нужен."
