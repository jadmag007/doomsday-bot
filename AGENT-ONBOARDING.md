# AGENT-ONBOARDING — быстрый старт для нового агента

Этот файл — готовый промпт для владельца: скопируй блок ниже, подставь PAT
и отдай любому ИИ-агенту с доступом к терминалу. Правила дублируют
PROJECT-MAP.md, «шпаргалка команд» — ниже.

---

## ПРОМПТ ДЛЯ АГЕНТА (копировать целиком, подставить PAT)

Ты работаешь над проектом «Doomsday Tyranny bot» — автоматизация
Telegram-игры (Python-воркер + web-панель, Android/Termux). Код и вся
рабочая среда лежат в приватном GitHub-репозитории.

Репо: https://github.com/jadmag007/doomsday-bot
Токен: <ВСТАВЬ_СЮДА_PAT>

### 1. Онбординг (сделай обязательно, до любой работы)

    export GH_PAT='<ВСТАВЬ_СЮДА_PAT>'
    git clone --branch workspace "https://jadmag007:${GH_PAT}@github.com/jadmag007/doomsday-bot.git" project
    cd project

    # (гигиена, опционально) убрать токен из .git/config:
    git remote set-url origin https://github.com/jadmag007/doomsday-bot.git

Затем прочитай по порядку:
1. `PROJECT-MAP.md` — карта проекта, архитектура, процесс релиза, грабли;
2. конец `worklog.md` — что делали предыдущие агенты и почему;
3. `MANIFEST.md` — краткая шпаргалка по песочнице.

### 2. Правила работы

- Ветка `workspace` — твоё рабочее место: код бота в корне + `research/`
  (справочник по игре), `scripts/` (инструменты, тесты), `upload/`,
  `logs-archive/`, документация. Коммить сюда всё, что делал.
- Ветка `main` — код, который тянет ЖИВОЕ устройство. Пуш туда только
  релизов после тестов (см. формат ниже).
- `config.json` в dev-песочнице содержит тестовые значения — при релизе
  в main его НЕ перезаписывать.

### 3. Формат возврата изменений (обязателен)

Коммит рабочей среды:

    git add -A
    git commit -m "workspace: <что сделал>"
    git push origin workspace

Релиз бота (только после зелёных тестов):

    git checkout -b rel origin/main
    git checkout workspace -- lib web bin boot service install.sh start.sh VERSION requirements.txt
    echo "X.Y.Z" > VERSION          # бамп версии
    python3 -m py_compile lib/*.py bin/doomsday
    node --check web/app.js
    git add -A && git commit -m "vX.Y.Z: <описание изменений>"
    git push origin rel:main
    git checkout workspace && git branch -D rel

Перед КАЖДЫМ пушем — скан утечки токена (должен быть пустым):

    grep -rF "$GH_PAT" . --exclude-dir=.git

Токен нигде не коммитить, не печатать целиком, не вносить в файлы репо.

В конце сессии — дополни `worklog.md` записью и запушь в workspace:

    ---

    Task ID: <короткий-id задачи>
    Agent: <какой ты агент>
    Task: <что просил владелец>

    Work Log:
    - <шаг 1>
    - <шаг 2>

    Stage Summary:
    - <итоги, ключевые решения, артефакты>

---

## Шпаргалка команд (для агента)

    # забрать свежие изменения:
    git pull --rebase origin workspace

    # запушить рабочую среду:
    git add -A && git commit -m "workspace: <суть>" && git push origin workspace

    # пуш с чистым remote (если делал гигиену из п.1):
    git push "https://jadmag007:${GH_PAT}@github.com/jadmag007/doomsday-bot.git" HEAD:workspace

    # посмотреть, что уже в main (чужие релизы):
    git fetch origin && git log origin/main --oneline -5

## Для владельца: как забрать работу агента к себе

В песочнице основного агента (Super Z):

    # обновить клон main и dev-дерево:
    cd /home/z/my-project/.mainwork/repo
    git fetch origin && git log origin/main --oneline -3
    # (синк в doomsday-bot/ — по фильтрам из scripts/release.sh, шаг 4)

    # обновить локальную копию workspace:
    cd /home/z/my-project/.mainwork/ws
    git fetch origin workspace && git reset --hard origin/workspace
    # worklog.md и scripts/ оттуда — источник правок чужого агента

## Для владельца: гигиена токенов

- Один агент = один токен (fine-grained, только это репо, только Contents RW).
- Закончил работу / агент скомпрометирован → GitHub → Settings →
  Developer settings → Fine-grained tokens → Revoke.
- Утёкший токен открывает только этот репо, но там config.json с
  api_id/api_hash — отзывай сразу.
