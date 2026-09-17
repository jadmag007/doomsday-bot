#!/bin/bash
# push_workspace_branch.sh — синк рабочей среды в ветку `workspace` приватного
# репо GitHub (jadmag007/doomsday-bot). Переиспользуемый: любой агент может
# запустить его после локальной работы, чтобы залить изменения песочницы.
#
# Ветка main НЕ трогается (её тянет устройство).
# Структура ветки workspace (= код бота из main + рабочие папки):
#   research/   — распакованный фронт игры (справочник API)
#   scripts/    — инструменты агента (релизы, тесты, PAT, диагностика)
#   upload/     — личные скриншоты владельца
#   logs-archive/ — бандлы логов устройства (диагностика ребутов)
#   PROJECT-MAP.md / MANIFEST.md / worklog.md — документация
# .secrets/gh.pat в репо НЕ попадает никогда (скан утечек перед пушем).
set -euo pipefail

ROOT=/home/z/my-project
PAT="$(bash "$ROOT/scripts/pat.sh" print)"
[ -n "$PAT" ] || { echo "FATAL: PAT пуст (bash scripts/pat.sh ensure)"; exit 1; }
CLEAN_URL="https://github.com/jadmag007/doomsday-bot.git"
PUSH_URL="https://jadmag007:${PAT}@github.com/jadmag007/doomsday-bot.git"
WORK="$ROOT/.mainwork/ws"
BRANCH=workspace

echo "=== 1. свежий клон main ==="
rm -rf "$WORK"
git clone --quiet "$PUSH_URL" "$WORK"
cd "$WORK"
git checkout --quiet -B "$BRANCH"
echo "main HEAD: $(git rev-parse --short HEAD)"

echo "=== 2. копирование рабочей среды (в явные подпапки) ==="
# код бота уже в клоне (из main); докидываем рабочие папки.
# ВАЖНО: БЕЗ конечных слэшей у источников-каталогов — иначе rsync вывалит
# содержимое папки в корень (инцидент 2026-09-17 с первой версией ветки).
mkdir -p "$WORK/research" "$WORK/scripts" "$WORK/upload" "$WORK/logs-archive"
rsync -a --delete --exclude='.git' --exclude='__pycache__' \
      "$ROOT/research/" "$WORK/research/"
rsync -a --exclude='.git' --exclude='__pycache__' \
      "$ROOT/scripts/" "$WORK/scripts/"
rsync -a "$ROOT/upload/" "$WORK/upload/"
# бандлы логов устройства (без git-обвязки .logswork/repo)
rsync -a "$ROOT/.logswork/repo/archive/" "$WORK/logs-archive/"
cp -f "$ROOT/PROJECT-MAP.md" "$ROOT/MANIFEST.md" "$ROOT/AGENT-ONBOARDING.md" "$ROOT/worklog.md" "$WORK/"

echo "=== 3. скан утечек PAT ==="
if grep -rqF "$PAT" "$WORK" --exclude-dir=.git; then
  echo "FATAL: строка PAT найдена в рабочем дереве — пуш отменён"; exit 1
fi
if grep -rqE 'github_pat_[A-Za-z0-9_]{20,}' "$WORK" --exclude-dir=.git; then
  echo "FATAL: найден паттерн github_pat_ в рабочем дереве — пуш отменён"; exit 1
fi
echo "утечек нет"

echo "=== 4. коммит ==="
git add -A
if git diff --cached --quiet; then
  echo "изменений нет — $BRANCH уже актуален"; exit 0
fi
git -c user.name="jadmag007" -c user.email="jadmag007@users.noreply.github.com" \
  commit --quiet -m "workspace: синк рабочей среды после ревизии 2026-09-17

Ревизия: scripts/ 102 -> 20 файлов (удалены мёртвые updater-тесты, старые
смоки v200-v250, vis-скрипты, разовые артефакты), релизные скрипты
объединены в параметризованный release.sh, research/ очищен от дублей.
main не тронут; ветка workspace — источник контекста для агентов.
См. PROJECT-MAP.md: онбординг, процесс релиза, правила PAT."
echo "коммит: $(git rev-parse --short HEAD), файлов в дереве: $(git ls-files | wc -l)"

echo "=== 5. push origin $BRANCH (--force: старая ветка была кривой) ==="
git push --quiet --force origin "$BRANCH"

echo "=== 6. проверка ==="
git ls-remote "$PUSH_URL" refs/heads/main refs/heads/workspace | sed "s|https://[^@]*@|https://|"
echo "OK"
