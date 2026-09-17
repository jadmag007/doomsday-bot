# Doomsday Tyranny bot — карта рабочей среды (2026-09-17, после ревизии)

## Текущее состояние
- Актуальный релиз: **v2.5.1** = коммит `4a7ce0b` на `main` в
  https://github.com/jadmag007/doomsday-bot
- Dev-дерево `doomsday-bot/` синхронизировано с main (4a7ce0b).
- Ветка `workspace` — рабочая среда агентов (код + research/scripts/upload/logs).
- Ревизия 2026-09-17: scripts/ 102 → 20 файлов, дубли и мёртвый код удалены,
  релизный процесс объединён в `release.sh`. Все 8 живых тестов зелёные.

## Критичные правила
1. **PAT**: лежит в `.secrets/gh.pat` (600). Восстановление —
   `bash scripts/pat.sh ensure`. НИКОГДА не пушить его в GitHub-репо
   (скан встроен в release.sh и push_workspace_branch.sh).
2. Пуш релизов — только через `bash scripts/release.sh <ver> "<msg>"`.
3. Пуш рабочей среды — `bash scripts/push_workspace_branch.sh` (ветка workspace,
   main не трогается).
4. После каждого крупного шага — commit в git песочницы (страховка от сбросов).
5. Значение токена в переписке/логах/worklog не светить.

## Что где лежит
| Путь | Содержимое |
|------|-----------|
| `doomsday-bot/` | Рабочее дерево бота (lib/, web/, bin/, boot/, service/, install.sh, start.sh) + рантайм локальных тестов |
| `.mainwork/repo/` | Клон с GitHub для релизов; `.mainwork/ws/` — временный клон workspace-синка |
| `.logswork/repo/archive/` | Бандлы логов устройства (bundle-*) → в workspace это `logs-archive/` |
| `scripts/` | Инструменты: pat.sh, release.sh, push_workspace_branch.sh, fetch_device_logs.sh, 6 research-скриптов, 8 тестов |
| `research/` | Фронт игры (chunks/, game_items.json, game_resources.json) |
| `upload/` | Личные скриншоты владельца |
| `worklog.md` | Полная история работы агентов |
| `.secrets/gh.pat` | GitHub PAT (jadmag007) |
| `download/` | Снапшот-архивы песочницы (full/LITE от 2026-09-17) |
