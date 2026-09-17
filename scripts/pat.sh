#!/bin/bash
# pat.sh — менеджер GitHub PAT для doomsday-bot (v1, 2026-09-16).
#
# ПРОБЛЕМА, которую решает: /home/z/.config и /tmp НЕ переживают перезапуск
# песочницы — PAT терялся, и пользователю приходилось присылать его заново.
# Единственное выживающее место — /home/z/my-project (персистентный каталог
# проекта). Поэтому ГЛАВНОЕ хранилище теперь /home/z/my-project/.secrets/gh.pat
# (каталог .secrets/ в .gitignore основного репо — токен в гит не попадает).
#
# Три копии (приоритет по порядку):
#   1) /home/z/my-project/.secrets/gh.pat   — ГЛАВНАЯ, персистентная
#   2) /home/z/.config/doomsday-bot/gh.pat  — зеркало (выживает не всегда)
#   3) /tmp/doomsday-gh.pat                 — зеркало (не выживает, но пусть будет)
#
# Команды:
#   pat.sh save <token>   — сохранить токен во все 3 места (chmod 600)
#   pat.sh ensure         — самолечение: найти токен где угодно и достлить
#                           его во все остальные места
#   pat.sh status         — показать, в каких местах токен есть
#   pat.sh print          — напечатать токен (для подстановки в скриптах)
#
# Проверка токена: pat.sh print | …  (никогда не логировать целиком!)
set -euo pipefail

SECRETS_DIR="/home/z/my-project/.secrets"
PRIMARY="$SECRETS_DIR/gh.pat"
MIRROR_CFG="/home/z/.config/doomsday-bot/gh.pat"
MIRROR_TMP="/tmp/doomsday-gh.pat"
ALL_LOCATIONS=("$PRIMARY" "$MIRROR_CFG" "$MIRROR_TMP")

_norm() { printf '%s' "$1" | tr -d '[:space:]'; }

_read_first_found() {
    for f in "${ALL_LOCATIONS[@]}"; do
        [ -s "$f" ] || continue
        local t; t="$(_norm "$(head -n1 "$f")")"
        case "$t" in
            github_pat_*|ghp_*|gho_*) printf '%s' "$t"; return 0 ;;
        esac
    done
    return 1
}

_save_all() {
    local token; token="$(_norm "$1")"
    case "$token" in
        github_pat_*|ghp_*|gho_*) ;;
        *) echo "pat.sh: это не похоже на GitHub-токен (github_pat_*/ghp_*)" >&2; exit 1 ;;
    esac
    mkdir -p "$SECRETS_DIR"
    local failed=()
    for f in "${ALL_LOCATIONS[@]}"; do
        if [ "$f" != "$PRIMARY" ]; then
            mkdir -p "$(dirname "$f")" 2>/dev/null || { failed+=("$f"); continue; }
        fi
        umask 177
        if printf '%s\n' "$token" > "$f" 2>/dev/null; then
            chmod 600 "$f" 2>/dev/null || true
        else
            failed+=("$f")
        fi
    done
    # главное хранилище обязано записаться
    [ -s "$PRIMARY" ] || { echo "pat.sh: НЕ УДАЛОСЬ записать $PRIMARY" >&2; exit 1; }
    echo "✓ токен сохранён (главное: $PRIMARY)"
    for f in "${failed[@]}"; do echo "    зеркало недоступно (не критично): $f"; done
}

CMD="${1:-ensure}"
case "$CMD" in
    save)
        [ -n "${2:-}" ] || { echo "Использование: pat.sh save <token>" >&2; exit 1; }
        _save_all "$2"
        ;;
    ensure)
        TOKEN="$(_read_first_found)" || {
            echo "pat.sh: токен НЕ найден ни в одном месте — попросите пользователя прислать PAT," >&2
            echo "        затем: bash scripts/pat.sh save <token>" >&2
            exit 1
        }
        _save_all "$TOKEN" >/dev/null
        echo "✓ ensure: токен на месте во всех 3 местах"
        ;;
    status)
        for f in "${ALL_LOCATIONS[@]}"; do
            if [ -s "$f" ] && [ -n "$(_norm "$(head -n1 "$f")")" ]; then
                echo "✓ есть     $f ($(wc -c < "$f" | tr -d ' ') байт)"
            else
                echo "✗ НЕТ      $f"
            fi
        done
        ;;
    print)
        TOKEN="$(_read_first_found)" || { echo "пат не найден" >&2; exit 1; }
        printf '%s' "$TOKEN"
        ;;
    *)
        echo "Использование: pat.sh [save <token>|ensure|status|print]" >&2
        exit 1
        ;;
esac
