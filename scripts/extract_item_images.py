# -*- coding: utf-8 -*-
"""Извлечь из бандла игры определения ресурсов: id, name, image (+ порядок как в игре)."""
import json
import re
import sys

SRC = "/home/z/my-project/research/index.js"

with open(SRC, encoding="utf-8", errors="replace") as f:
    js = f.read()

# Определения вида {id:"alloy1",name:"Alloy",image:"/assets/items/alloy1.webp",craftImage:...
pat = re.compile(
    r'\{id:"([a-zA-Z0-9_]+)",name:"([^"]*)",image:"(/assets/items/[a-zA-Z0-9_]+\.webp)"'
    r'(?:,craftImage:"(/assets/items/[a-zA-Z0-9_]+\.webp)")?')
seen = {}
order = []
for m in pat.finditer(js):
    rid, name, img, cimg = m.groups()
    if rid not in seen:
        order.append(rid)
    seen[rid] = {"id": rid, "name": name, "image": img, "craft": cimg or ""}

print(f"найдено определений: {len(seen)}")
sys.path.insert(0, "/home/z/my-project/doomsday-bot")
from lib.game_data import NAMES, RU_NAMES, CAPACITIES  # noqa: E402

ours = set(NAMES) | set(CAPACITIES)
missing_in_game = sorted(ours - set(seen))
extra_in_game = sorted(set(seen) - ours)
print("нет в игре (наши лишние):", missing_in_game)
print("есть в игре, нет у нас:", extra_in_game)

# имя в игре vs наше английское имя
diffs = [(rid, seen[rid]["name"], NAMES.get(rid)) for rid in seen
         if rid in NAMES and seen[rid]["name"] != NAMES.get(rid)]
print("расхождения имён (rid, игра, наши):")
for d in diffs:
    print("  ", d)

with open("/home/z/my-project/research/game_items.json", "w", encoding="utf-8") as f:
    json.dump([seen[r] for r in order], f, ensure_ascii=False, indent=1)
print("порядок как в игре (первые 15):", order[:15])
print("сохранено: research/game_items.json")
