#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Анализ чанков игры Doomsday Tyranny: найти все вызовы callable-API.

Механика: index-vV5GEdeZ.js экспортирует функцию запроса как `P`
(см. wait-for-confirmation: import{P as n} ... n("getTransactionStatus",{payload:a})).
В каждом чанке извлекаем локальный псевдоним P из импорта и ищем вызовы X("имя", ...).
"""
import os
import re
import sys
from collections import defaultdict

RESEARCH = "/home/z/my-project/research"
CHUNKS = os.path.join(RESEARCH, "chunks")
INDEX = os.path.join(RESEARCH, "index.js")

# import{A as x,B as y,P as z,...}from"./index-vV5GEdeZ.js"
IMPORT_RE = re.compile(
    r'import\s*\{([^}]*)\}\s*from\s*"\./index-vV5GEdeZ\.js"')
SPEC_RE = re.compile(r'(\w+)\s+as\s+(\w+)')


def aliases_of(path, wanted_export="P"):
    """Вернуть список локальных имён, под которыми импортирован wanted_export."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            src = f.read()
    except OSError:
        return out
    for m in IMPORT_RE.finditer(src):
        for spec in SPEC_RE.finditer(m.group(1)):
            exported, local = spec.group(1), spec.group(2)
            if exported == wanted_export:
                out.append(local)
    return out


def main():
    calls = defaultdict(set)   # имя функции -> set(файл)
    contexts = {}              # имя функции -> пример вызова

    files = [("index", INDEX)] + [
        (f, os.path.join(CHUNKS, f)) for f in sorted(os.listdir(CHUNKS))
        if f.endswith(".js")
    ]

    for label, path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except OSError:
            continue
        # ищем все локальные псевдонимы экспорта P
        for alias in set(aliases_of(path)) | ({"P"} if label == "index" else set()):
            # X("name"  или  X("name",
            for m in re.finditer(
                    re.escape(alias) + r'\(\s*"([a-zA-Z0-9_]{3,50})"\s*(?:,|\))', src):
                name = m.group(1)
                calls[name].add(label)
                if name not in contexts:
                    s = max(0, m.start() - 120)
                    contexts[name] = src[s:m.end() + 120].replace("\n", " ")

    print(f"Найдено callable-функций: {len(calls)}\n")
    for name in sorted(calls):
        files_short = ",".join(sorted(calls[name]))[:60]
        print(f"  {name:32s} [{files_short}]")
    print("\n===== контексты вызовов =====")
    for name in sorted(contexts):
        print(f"\n--- {name} ---")
        print("  " + contexts[name][:400])

    # дополнительно: ищем строки-имена, переданные через переменные (Bc(e,t) в index)
    print("\n===== строки-кандидаты имён функций (index.js, кавычки в вызовах) =====")
    with open(INDEX, encoding="utf-8", errors="ignore") as f:
        src = f.read()
    for m in re.finditer(r'(?:Bc|cN)\(\s*([a-zA-Z_$][\w$]*)\s*[,)]', src):
        var = m.group(1)
        # где переменная определена как строка
        for d in re.finditer(
                re.escape(var) + r'\s*=\s*"([a-zA-Z0-9_]{3,50})"', src):
            print(f"  {var} = \"{d.group(1)}\"")


if __name__ == "__main__":
    sys.exit(main())
