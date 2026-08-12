#!/usr/bin/env python3
"""Database layer for StegoBot — all SQLite3 access goes through here."""

import sqlite3
import fnmatch
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = '/opt/stegobot/stegobot.db'
_local = threading.local()


def _conn():
    if not getattr(_local, 'conn', None):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_schema():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            hostmask   TEXT UNIQUE NOT NULL,
            level      TEXT NOT NULL CHECK(level IN ('peon','admin')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS channels (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT UNIQUE NOT NULL,
            currentTopic TEXT,
            first_joined TIMESTAMP,
            last_rejoin  TIMESTAMP,
            num_users    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS servers (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            host     TEXT UNIQUE NOT NULL,
            port     INTEGER DEFAULT 6667,
            priority INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS web_sessions (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used       INTEGER DEFAULT 0
        );
    """)
    c.commit()
    c.close()


# ── Config ─────────────────────────────────────────────────────────────────

def cfg_get(key, default=None):
    row = _conn().execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def cfg_set(key, value):
    _conn().execute('INSERT OR REPLACE INTO config(key,value) VALUES(?,?)', (key, str(value)))
    _conn().commit()


def cfg_all():
    rows = _conn().execute('SELECT key,value FROM config ORDER BY key').fetchall()
    return {r['key']: r['value'] for r in rows}


def cfg_delete(key):
    _conn().execute('DELETE FROM config WHERE key=?', (key,))
    _conn().commit()


# ── Users ───────────────────────────────────────────────────────────────────

def user_level(hostmask):
    """Return 'peon'|'admin' if hostmask matches any stored pattern, else None."""
    rows = _conn().execute('SELECT hostmask, level FROM users').fetchall()
    for r in rows:
        if fnmatch.fnmatch(hostmask.lower(), r['hostmask'].lower()):
            return r['level']
    return None


def user_add(hostmask, level):
    _conn().execute('INSERT OR REPLACE INTO users(hostmask,level) VALUES(?,?)', (hostmask, level))
    _conn().commit()


def user_list():
    return _conn().execute('SELECT id,hostmask,level,created_at FROM users ORDER BY id').fetchall()


def user_delete(hostmask):
    _conn().execute('DELETE FROM users WHERE hostmask=?', (hostmask,))
    _conn().commit()


# ── Channels ────────────────────────────────────────────────────────────────

def chan_list():
    return [r['name'] for r in _conn().execute('SELECT name FROM channels ORDER BY name')]


def chan_add(name):
    now = datetime.utcnow().isoformat()
    _conn().execute(
        'INSERT OR IGNORE INTO channels(name,first_joined,last_rejoin) VALUES(?,?,?)',
        (name.lower(), now, now)
    )
    _conn().commit()


def chan_remove(name):
    _conn().execute('DELETE FROM channels WHERE name=?', (name.lower(),))
    _conn().commit()


def chan_set_topic(name, topic):
    _conn().execute('UPDATE channels SET currentTopic=? WHERE name=?', (topic, name.lower()))
    _conn().commit()


def chan_set_users(name, n):
    _conn().execute('UPDATE channels SET num_users=?,last_rejoin=? WHERE name=?',
                    (n, datetime.utcnow().isoformat(), name.lower()))
    _conn().commit()


def chan_all():
    return _conn().execute(
        'SELECT name,currentTopic,first_joined,last_rejoin,num_users FROM channels ORDER BY name'
    ).fetchall()


# ── Servers ─────────────────────────────────────────────────────────────────

def srv_list():
    return _conn().execute('SELECT host,port,priority FROM servers ORDER BY priority').fetchall()


def srv_next():
    """Return (host, port) of highest-priority server."""
    r = _conn().execute('SELECT host,port FROM servers ORDER BY priority LIMIT 1').fetchone()
    return (r['host'], r['port']) if r else (None, None)


def srv_rotate(connected_host):
    """Move connected_host to bottom of priority list, shift others up."""
    rows = _conn().execute('SELECT id,host FROM servers ORDER BY priority').fetchall()
    ordered = [r for r in rows if r['host'].lower() != connected_host.lower()]
    tail   = [r for r in rows if r['host'].lower() == connected_host.lower()]
    ordered += tail
    for i, r in enumerate(ordered):
        _conn().execute('UPDATE servers SET priority=? WHERE id=?', (i, r['id']))
    _conn().commit()


def srv_add(host, port=6667):
    max_p = _conn().execute('SELECT MAX(priority) FROM servers').fetchone()[0]
    _conn().execute('INSERT OR IGNORE INTO servers(host,port,priority) VALUES(?,?,?)',
                    (host.lower(), port, (max_p or 0) + 1))
    _conn().commit()


def srv_delete(host):
    _conn().execute('DELETE FROM servers WHERE host=?', (host.lower(),))
    _conn().commit()


# ── Web sessions ─────────────────────────────────────────────────────────────

def session_create(token, email):
    _conn().execute('INSERT INTO web_sessions(token,email) VALUES(?,?)', (token, email))
    _conn().commit()


def session_consume(token):
    """Mark token used and return email, or None if invalid/expired/already used."""
    r = _conn().execute(
        "SELECT email,used FROM web_sessions WHERE token=? AND created_at>datetime('now','-1 hour')",
        (token,)
    ).fetchone()
    if r and not r['used']:
        _conn().execute('UPDATE web_sessions SET used=1 WHERE token=?', (token,))
        _conn().commit()
        return r['email']
    return None


# ── Arbitrary query (admin command) ─────────────────────────────────────────

def run_query(sql):
    """Execute arbitrary SQL and return (columns, rows).  SELECT only for safety."""
    c = _conn()
    cur = c.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, [list(r) for r in rows]
