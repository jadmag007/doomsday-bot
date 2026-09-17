#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачать все чанки игры и найти callable-функции обмена/продажи."""
import re
import os
import ssl
import urllib.request

ORIGIN = "https://telegram-miracle-f1779.web.app"
OUT = "/home/z/my-project/research"
ctx = ssl.create_default_context()
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) Chrome/124.0 Mobile Safari/537.36"}

chunks = open(os.path.join(OUT, "_chunks.txt")).read().split()
main = open(os.path.join(OUT, "index-vV5GEdeZ.js"), encoding="utf-8").read()

texts = {}
for c in chunks:
    url = f"{ORIGIN}/{c}"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            t = r.read().decode("utf-8", errors="replace")
        texts[c] = t
        fn = os.path.join(OUT, c.rsplit("/", 1)[-1])
        if not os.path.exists(fn):
            open(fn, "w", encoding="utf-8").write(t)
    except Exception as e:
        print(f"FAIL {c}: {e}")
print(f"скачано {len(texts)}/{len(chunks)} чанков")

# 1) как чанки импортируют Bc из главного бандла
aliases = set()
for t in texts.values():
    for m in re.finditer(r'import\{([^}]{0,400})\}\s*from\s*"\./(index-vV5GEdeZ\.js)"', t):
        # Bc as X  или  X as Bc
        for part in m.group(1).split(","):
            part = part.strip()
            if part.endswith(" as Bc"):
                aliases.add(part[: -len(" as Bc")].strip())
            elif part == "Bc":
                aliases.add("Bc")
print("алиасы Bc в чанках:", sorted(aliases))

# 2) все вызовы <alias>("name") во всех файлах (включая главный)
from collections import Counter
calls = Counter()
pat_alias = None
blob_all = main + "\n" + "\n".join(texts.values())
if aliases:
    a = "|".join(sorted(re.escape(a) for a in aliases))
    pat_alias = re.compile(r'(?:await\s+)?(' + a + r')\(\s*["\']([A-Za-z0-9_]{2,60})["\']')
    for m in pat_alias.finditer(blob_all):
        calls[m.group(2)] += 1
print("\n=== Вызовы callable через алиасы Bc ===")
for n, c2 in sorted(calls.items()):
    print(f"  {n}  x{c2}")

# 3) страховка: известные/подозрительные имена как строки в любом месте
sus = set()
for m in re.finditer(r'["\']([A-Za-z0-9_]{3,50})["\']', blob_all):
    s = m.group(1)
    low = s.lower()
    if re.match(r'^[a-z]+([A-Z][a-z0-9]+)+$', s) and any(
            k in low for k in ("sell", "exchange", "request", "install", "data",
                               "offer", "claim", "buy", "market", "trade", "convert",
                               "memory", "ddt")):
        sus.add(s)
print("\n=== Подозрительные camelCase строки ===")
for n in sorted(sus):
    print(" ", n)
