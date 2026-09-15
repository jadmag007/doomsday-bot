#!/data/data/com.termux/files/usr/bin/sh
# ============================================================
#  doomsday-bot — стартёр: обновление из GitHub + запуск всего.
#
#  Запуск вручную:      sh ~/doomsday-bot/start.sh
#  После перезагрузки:  автоматически (Termux:Boot, см. README)
#  По расписанию:       можно повесить в cron доп. строку (не обязательно)
# ============================================================
set -u

APP_DIR="${DOOMSDAY_BOT_DIR:-$HOME/doomsday-bot}"
SRC_DIR="${DOOMSDAY_SRC_DIR:-$HOME/doomsday-src}"
[ -n "${PREFIX:-}" ] && export PATH="$PREFIX/bin:$PATH"

say() { printf "▸ %s\n" "$*"; }
ok()  { printf "✓ %s\n" "$*"; }

# ---------- 1. обновление из GitHub (если исходники — git-клон) ----------
if [ -d "$SRC_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    say "Проверяю обновления на GitHub…"
    if (cd "$SRC_DIR" && git pull --ff-only -q 2>/dev/null); then
        NEW_VER="$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo '?')"
        CUR_VER="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?')"
        if [ "$NEW_VER" != "$CUR_VER" ] && [ -f "$SRC_DIR/install.sh" ]; then
            say "Новая версия: $NEW_VER (установлена: $CUR_VER) — переустанавливаю…"
            if sh "$SRC_DIR/install.sh" >/dev/null 2>&1; then
                ok "обновлён до $NEW_VER"
            else
                say "переустановка не удалась — продолжаю на текущей версии"
            fi
        else
            say "обновлений нет ($CUR_VER)"
        fi
    else
        say "git pull не удался (нет сети?) — продолжаю на текущей версии"
    fi
fi

# ---------- 2. wake-lock и сервисы ----------
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock >/dev/null 2>&1 &
if command -v sv >/dev/null 2>&1; then
    sv up doomsday-web >/dev/null 2>&1 || sv restart doomsday-web >/dev/null 2>&1
    sv up cronie >/dev/null 2>&1 || true
    ok "сервисы подняты (doomsday-web, cronie)"
else
    [ -x "$APP_DIR/bin/doomsday" ] && "$APP_DIR/bin/doomsday" web >/dev/null 2>&1 &
    say "termux-services нет — веб-панель запущена в фоне без сервиса"
fi

# ---------- 3. контрольный проход ----------
if [ -x "$APP_DIR/bin/doomsday" ]; then
    "$APP_DIR/bin/doomsday" cron >/dev/null 2>&1 &
    ok "контрольный проход запущен"
fi

WEB_PORT="$(grep -o '"port"[[:space:]]*:[[:space:]]*[0-9]*' "$APP_DIR/config.json" 2>/dev/null | head -n1 | grep -o '[0-9]*$')"
ok "doomsday-bot работает. Веб-панель: http://127.0.0.1:${WEB_PORT:-8080}"
