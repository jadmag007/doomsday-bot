#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Извлечь статическую таблицу ресурсов игры Doomsday Tyranny из index.js:
id, name, updateCapacityCount (ёмкости складов по уровням).
"""
import json
import re

SRC = "/home/z/my-project/research/index.js"
src = open(SRC, encoding="utf-8", errors="ignore").read()

# resource:{id:"...",name:"...",...},store:{...,updateCapacityCount:[...],...}
out = {}
for m in re.finditer(r'resource:\{id:"([^"]+)",name:"([^"]+)"', src):
    rid, name = m.group(1), m.group(2)
    tail = src[m.end():m.end() + 4000]
    cap = re.search(r'updateCapacityCount:\[([^\]]*)\]', tail)
    lvl = re.search(r'levelStore(?:defaults)?[:=](\d+)', tail)
    produce = re.search(r'produceTime:(\d+)', tail)
    caps = None
    if cap:
        try:
            caps = json.loads("[" + cap.group(1).replace("e+", "e") + "]")
        except ValueError:
            caps = cap.group(1)
    out[rid] = {"name": name, "capacities": caps,
                "produce_time": int(produce.group(1)) if produce else None}

print(f"Найдено ресурсов: {len(out)}")
for rid, info in out.items():
    caps = info["capacities"]
    cap_str = (f"[{caps[0]}..{caps[-1]}] x{len(caps)}" if isinstance(caps, list) and caps else str(caps)[:40])
    print(f"  {rid:16s} {info['name']:22s} caps={cap_str} t={info['produce_time']}")

with open("/home/z/my-project/research/game_resources.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("\nСохранено: research/game_resources.json")
