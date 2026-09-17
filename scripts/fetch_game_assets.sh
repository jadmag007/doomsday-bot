#!/usr/bin/env bash
# Скачать графические ассеты игры Doomsday Tyranny для веб-панели.
set -u
ORIG="https://telegram-miracle-f1779.web.app"
DST="/home/z/my-project/research/assets"
ITEMS_JSON="/home/z/my-project/research/game_items.json"
WEB_ITEMS="/home/z/my-project/doomsday-bot/web/assets/items"
mkdir -p "$DST/fonts" "$DST/items" "$WEB_ITEMS"

# 1) иконки ресурсов — под именами НАШИХ rid (game_items.json уже с путями игры)
/home/z/.venv/bin/python3 - <<'PY'
import json, os, subprocess, sys
items = json.load(open('/home/z/my-project/research/game_items.json'))
web_items = '/home/z/my-project/doomsday-bot/web/assets/items'
ok = fail = 0
for it in items:
    url = 'https://telegram-miracle-f1779.web.app' + it['image']
    dst = os.path.join(web_items, it['id'] + '.webp')
    r = subprocess.run(['curl', '-s', '--max-time', '25', '-o', dst, url])
    good = r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 200
    ok += good; fail += (not good)
    if not good:
        print('FAIL', it['id'], url)
print(f'иконки: ok={ok} fail={fail}')
PY

# 2) шрифты и логотип
for f in fonts/europe-normal.woff fonts/europe-bold.woff \
         fonts/Iosevka-Regular.woff2 fonts/Iosevka-SemiBold.woff2 \
         logo.webp favicon.ico; do
  curl -s --max-time 25 -o "$DST/$f" "$ORIG/$f" && echo "ok $f $(stat -c%s "$DST/$f")" || echo "FAIL $f"
done

# 3) итог
echo "--- web/assets/items ---"
ls "$WEB_ITEMS" | wc -l
du -sh "$WEB_ITEMS" "$DST/fonts"
