#!/data/data/com.termux/files/usr/bin/sh
# ============================================================
#  Doomsday Tyranny Bot — автоустановка для Termux (Android)
#  Запуск (после распаковки архива в ~/doomsday-src):
#    bash ~/doomsday-src/install.sh
# ============================================================
set -u

RED="\033[0;31m"; GREEN="\033[0;32m"; YEL="\033[1;33m"; CYA="\033[0;36m"; OFF="\033[0m"
say()  { printf "${CYA}▸${OFF} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${OFF} %s\n" "$*"; }
warn() { printf "${YEL}!${OFF} %s\n" "$*"; }
die()  { printf "${RED}✘ %s${OFF}\n" "$*" >&2; exit 1; }

# ---------- окружение ----------
[ -n "${PREFIX:-}" ] || die "Запускайте только в Termux (переменная PREFIX не найдена)"
case "$(uname -o 2>/dev/null || uname -s)" in
    Android*) ;;
    *) warn "Похоже, это не Android/Termux — продолжаю, но работа не гарантирована" ;;
esac

SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
APP_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"
CRON_MARK="# doomsday-bot"
SVC_DIR="$PREFIX/var/service/doomsday-web"
BOOT_DIR="$HOME/.termux/boot"

printf "${CYA}"
echo "╔══════════════════════════════════════════════╗"
echo "║   ☢  Doomsday Tyranny Bot — установка        ║"
echo "║   ребут 12ч · скан ресурсов · веб-панель     ║"
echo "╚══════════════════════════════════════════════╝"
printf "${OFF}"
echo "Источник:  $SRC_DIR"
echo "Установка: $APP_DIR"
echo ""

# ---------- 1. пакеты ----------
say "Пакеты Termux (python, cronie, termux-api, termux-services)…"
pkg install -y python cronie termux-api termux-services termux-tools unzip >/dev/null 2>&1 \
    || pkg install -y python cronie termux-services unzip >/dev/null 2>&1 \
    || warn "pkg install завершился с ошибкой — проверьте сеть (pkg update)"
command -v python >/dev/null 2>&1 || die "python не установлен. Выполните: pkg install python"
command -v crontab >/dev/null 2>&1 || warn "crontab не найден — планировщик не будет установлен автоматически"
ok "пакеты готовы"

# ---------- 2. доступ к Загрузкам ----------
say "Доступ к Загрузкам (для автообновления из архива)…"
DL_AUTO="/storage/emulated/0/Download"
DL_HOME="$HOME/storage/downloads"
if [ ! -d "$DL_HOME" ] && [ ! -d "$DL_AUTO" ]; then
    warn "Нет доступа к общей памяти. Сейчас откроется запрос разрешений — нажмите «Разрешить»"
    termux-setup-storage 2>/dev/null
    sleep 3
fi
if [ -d "$DL_HOME" ] || [ -d "$DL_AUTO" ]; then
    ok "Загрузки доступны"
else
    warn "Загрузки недоступны — автообновление из архива будет ждать termux-setup-storage"
fi

# ---------- 3. копирование кода ----------
say "Копирую код в $APP_DIR…"
REINSTALL=0
# перенос настроек из репозитория/исходников (клон на новом устройстве):
# если в каталоге с исходниками есть config.json, а в установке ещё нет — берём его
mkdir -p "$APP_DIR"
if [ ! -f "$APP_DIR/config.json" ] && [ -f "$SRC_DIR/config.json" ]; then
    cp -f "$SRC_DIR/config.json" "$APP_DIR/config.json"
    ok "config.json перенесён из исходников (настройки api_id/интервалов подхвачены)"
fi
[ -f "$APP_DIR/config.json" ] && REINSTALL=1
if [ "$SRC_DIR" = "$APP_DIR" ]; then
    warn "Исходники и установка — один и тот же каталог (git-клон напрямую): копирование пропущено"
else
    for entry in lib web bin boot service VERSION requirements.txt README.md install.sh update.sh uninstall.sh; do
        [ -e "$SRC_DIR/$entry" ] || continue
        case "$entry" in
            lib|web|bin|boot|service)
                rm -rf "$APP_DIR/$entry"
                cp -R "$SRC_DIR/$entry" "$APP_DIR/$entry" ;;
            *) cp -f "$SRC_DIR/$entry" "$APP_DIR/$entry" ;;
        esac
    done
fi
chmod +x "$APP_DIR/bin/doomsday" "$APP_DIR/update.sh" "$APP_DIR/uninstall.sh" \
    "$APP_DIR/service/run" "$APP_DIR/boot/doomsday-boot.sh" 2>/dev/null
mkdir -p "$APP_DIR/logs" "$APP_DIR/session" "$APP_DIR/reports" "$APP_DIR/.updates"
chmod 700 "$APP_DIR/session" 2>/dev/null
[ "$REINSTALL" = "1" ] && ok "обновление поверх существующей установки (config/state сохранены)" || ok "скопировано"

# ---------- 4. venv + зависимости ----------
say "Python-окружение и Telethon (первый раз — пару минут)…"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    python -m venv "$APP_DIR/venv" >/dev/null 2>&1 || die "не удалось создать venv"
fi
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
    || die "pip install telethon провалился (проверьте интернет)"
ok "зависимости установлены"

# ---------- 5. первичная настройка ----------
if [ "$REINSTALL" = "0" ] || [ "${1:-}" = "--setup" ]; then
    say "Мастер настройки (api_id/api_hash, интервалы)…"
    "$APP_DIR/bin/doomsday" setup || warn "настройка прервана — позже: doomsday setup"
fi

# ---------- 6. Termux:Boot ----------
if [ -d "$HOME/.termux" ] || command -v termux-info >/dev/null 2>&1; then
    say "Автозапуск после перезагрузки (Termux:Boot)…"
    mkdir -p "$BOOT_DIR"
    cp -f "$APP_DIR/boot/doomsday-boot.sh" "$BOOT_DIR/90-doomsday-boot.sh"
    chmod +x "$BOOT_DIR/90-doomsday-boot.sh"
    ok "boot-скрипт установлен"
    command -v termux-wake-lock >/dev/null 2>&1 || warn "Установите приложение Termux:Boot (F-Droid/GitHub), иначе автозапуск не сработает"
fi

# ---------- 7. сервис веб-панели ----------
say "Сервис веб-панели (termux-services)…"
if [ -d "$PREFIX/var/service" ] || pkg list-installed 2>/dev/null | grep -q termux-services; then
    mkdir -p "$SVC_DIR"
    cp -f "$APP_DIR/service/run" "$SVC_DIR/run"
    chmod +x "$SVC_DIR/run"
    mkdir -p "$SVC_DIR/log"
    printf '#!/data/data/com.termux/files/usr/bin/sh\nexec svlogd -r "" .\n' > "$SVC_DIR/log/run" 2>/dev/null
    chmod +x "$SVC_DIR/log/run" 2>/dev/null
    sv up doomsday-web >/dev/null 2>&1 || sv restart doomsday-web >/dev/null 2>&1 \
        || warn "не удалось поднять сервис сейчас — перезапустите Termux"
    ok "сервис doomsday-web запущен"
else
    warn "termux-services не установлен — веб-панель запускайте вручную: doomsday web"
fi

# ---------- 8. cronie ----------
say "Планировщик cronie…"
if command -v crontab >/dev/null 2>&1; then
    RUNLINE="$CRON_MARK */10 * * * * $APP_DIR/bin/doomsday cron >> $APP_DIR/logs/cron.log 2>&1"
    CRON_TMP=$(mktemp)
    crontab -l 2>/dev/null | grep -v "$CRON_MARK" > "$CRON_TMP" || true
    echo "$RUNLINE" >> "$CRON_TMP"
    crontab "$CRON_TMP" && ok "cron: проход каждые 10 минут"
    rm -f "$CRON_TMP"
    command -v sv >/dev/null 2>&1 && { sv up cronie >/dev/null 2>&1 || sv restart cronie >/dev/null 2>&1; }
else
    warn "crontab недоступен — установите cronie: pkg install cronie termux-services"
fi

# ---------- 9. глобальная команда doomsday ----------
say "Глобальная команда doomsday…"
if [ -d "$PREFIX/bin" ]; then
    ln -sf "$APP_DIR/bin/doomsday" "$PREFIX/bin/doomsday"
    ok "doomsday доступна из любого места (симлинк в \$PREFIX/bin)"
fi

# ---------- 10. Termux:API ----------
say "Проверка push-уведомлений…"
if command -v termux-notification >/dev/null 2>&1; then
    ok "termux-notification доступен"
    warn "Убедитесь, что установлено приложение Termux:API (F-Droid или GitHub — тот же источник, что и Termux!)"
else
    warn "termux-api не установлен: pkg install termux-api + приложение Termux:API"
fi

# ---------- 11. контроль ----------
"$APP_DIR/bin/doomsday" selftest || warn "selftest нашёл проблемы"

echo ""
printf "${GREEN}══════════════════════════════════════════════${OFF}\n"
echo " Установка завершена. Дальнейшие шаги:"
echo ""
echo "   1) doomsday login        # вход в Telegram (код + 2FA)"
echo "   2) doomsday check        # диагностика всех компонентов"
echo "   3) doomsday discover     # найти API игры (отчёт в reports/)"
echo "   4) браузер → http://127.0.0.1:8080"
echo ""
echo " Веб-панель:  $([ -d "$SVC_DIR" ] && echo "сервис doomsday-web (уже запущен)" || echo "doomsday web")"
echo " Команды:     doomsday status | scan | reboot | log | update"
echo ""
echo " ⚠ Обязательно в Android: Настройки → Приложения → Termux →"
echo "   Батарея → «Без ограничений» (иначе Android усыпит cron ночью)."
echo ""
[ "$REINSTALL" = "0" ] && [ -d "$SRC_DIR" ] && [ "${SRC_DIR#$HOME}" != "$SRC_DIR" ] && {
    say "Исходники в $SRC_DIR больше не нужны — удалите: rm -rf $SRC_DIR"
}
exit 0
