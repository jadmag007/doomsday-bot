#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск эндпоинтов обмена/продажи в JS-бандле игры Doomsday Tyranny.

Скачивает index.html Mini App + все <script src>, ищет:
- имена Firebase callable-функций (httpsCallable / functions.httpsCallable)
- строки-кандидаты: sell, exchange, trade, convert, install, data, memory
- контекст вокруг найденного (50 символов до/после)
"""
import re
import ssl
import sys
import urllib.request

ORIGIN = "https://telegram-miracle-f1779.web.app"
OUT = "/home/z/my-project/research"

import os
os.makedirs(OUT, exist_ok=True)

ctx = ssl.create_default_context()
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) Chrome/124.0 Mobile Safari/537.36"}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


html = fetch(ORIGIN + "/index.html")
open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
print(f"index.html: {len(html)} bytes")

srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)
links = re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', html, re.I)
print("scripts:", srcs)
print("links:", links[:10])

urls = []
for s in srcs + links:
    if s.startswith("http"):
        urls.append(s)
    elif s.startswith("/"):
        urls.append(ORIGIN + s)
    else:
        urls.append(ORIGIN + "/" + s.lstrip("./"))

corpus = {}
for u in dict.fromkeys(urls):
    try:
        t = fetch(u)
        corpus[u] = t
        name = u.rsplit("/", 1)[-1].replace("?", "_")
        open(os.path.join(OUT, name), "w", encoding="utf-8").write(t)
        print(f"  {u}: {len(t)} bytes")
    except Exception as e:
        print(f"  {u}: FAIL {e}")

blob = "\n".join(corpus.values()) + html
print(f"\nВсего JS: {len(blob)} байт")

# 1) Firebase callable: httpsCallable(xxx, 'name') / httpsCallable(getFunctions(), "name")
callable_names = set()
for m in re.finditer(r"httpsCallable\(([^)]{0,200}?)['\"]([A-Za-z0-9_]{2,60})['\"]", blob):
    callable_names.add(m.group(2))
for m in re.finditer(r"functions\s*\.\s*httpsCallable\(\s*['\"]([A-Za-z0-9_]{2,60})['\"]", blob):
    callable_names.add(m.group(1))
print("\n=== Firebase callable функции ===")
for n in sorted(callable_names):
    print(" ", n)

# 2) все слова-кандидаты как отдельные строки в кавычках
cand = set()
for m in re.finditer(r"['\"]([A-Za-z0-9_\-]{3,50})['\"]", blob):
    s = m.group(1)
    low = s.lower()
    if any(k in low for k in ("sell", "exchange", "trade", "convert", "install",
                              "memory", "data", "ddt", "prem", "market", "shop")):
        cand.add(s)
print("\n=== Строки-кандидаты (sell/exchange/trade/convert/install/data/ddt...) ===")
for n in sorted(cand):
    print(" ", n)
