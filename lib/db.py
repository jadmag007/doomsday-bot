# -*- coding: utf-8 -*-
"""Состояние и журнал: SQLite (WAL). Потокобезопасно (один лок на запись)."""
import json
import os
import sqlite3
import threading
import datetime

from . import paths

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  pid INTEGER,
  started TEXT NOT NULL,
  finished TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  log TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  name TEXT NOT NULL,
  current REAL,
  maximum REAL,
  state TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_resources_name ON resources (name, id DESC);
"""


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(paths.DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(paths.DB_PATH, check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=30000")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def kv_get(key: str, default=None):
    with _lock:
        row = _connect().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return row["value"]


def kv_set(key: str, value) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        c.commit()


# ---------------- события ----------------

def event(kind: str, title: str, body: str = "", severity: str = "info") -> int:
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO events (ts, kind, severity, title, body) VALUES (?,?,?,?,?)",
            (now_iso(), kind, severity, title, body[:8000]),
        )
        c.commit()
        event_id = cur.lastrowid
        # журнал не пухнем: храним последние 800
        c.execute(
            "DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0) - 800 FROM events)"
        )
        c.commit()
    return event_id


def events_list(limit: int = 50, kind: str = "", offset: int = 0) -> list:
    q = "SELECT * FROM events"
    args = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with _lock:
        rows = _connect().execute(q, args).fetchall()
    return [dict(r) for r in rows]


# ---------------- запуски (ручные действия из веб-панели) ----------------

def run_start(action: str, pid: int = None) -> int:
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO runs (action, pid, started, status) VALUES (?,?,?,?)",
            (action, pid, now_iso(), "running"),
        )
        c.commit()
        return cur.lastrowid


def run_finish(run_id: int, status: str, log: str = "") -> None:
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE runs SET finished=?, status=?, log=? WHERE id=?",
            (now_iso(), status, log[-16000:], run_id),
        )
        c.execute(
            "DELETE FROM runs WHERE id < (SELECT COALESCE(MAX(id),0) - 200 FROM runs)"
        )
        c.commit()


def run_get(run_id: int) -> dict:
    with _lock:
        row = _connect().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else {}


def runs_list(limit: int = 20) -> list:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def running_run() -> dict:
    with _lock:
        row = _connect().execute(
            "SELECT * FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {}


# ---------------- ресурсы ----------------

def resource_snapshot(items: list) -> None:
    ts = now_iso()
    with _lock:
        c = _connect()
        c.executemany(
            "INSERT INTO resources (ts, name, current, maximum, state) VALUES (?,?,?,?,?)",
            [(ts, it.get("name", "?"), _f(it.get("current")), _f(it.get("max")), it.get("state", "")) for it in items],
        )
        # историю храним 14 дней
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=14)).isoformat(timespec="seconds")
        c.execute("DELETE FROM resources WHERE ts < ?", (cutoff,))
        c.commit()


def resources_latest() -> list:
    with _lock:
        rows = _connect().execute(
            "SELECT r.* FROM resources r JOIN (SELECT name, MAX(id) AS mid FROM resources GROUP BY name) t "
            "ON r.id = t.mid ORDER BY r.name"
        ).fetchall()
    return [dict(r) for r in rows]


def resources_history(hours: int = 24) -> dict:
    since = (datetime.datetime.now() - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
    with _lock:
        rows = _connect().execute(
            "SELECT ts, name, current, maximum FROM resources WHERE ts >= ? ORDER BY id",
            (since,),
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["name"], []).append({"ts": r["ts"], "current": r["current"], "max": r["maximum"]})
    return out


def _f(v):
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


# ---------------- служебное ----------------

def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.commit()
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None
