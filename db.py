#!/usr/bin/env python3
"""Database layer for StegoBot — all SQLite3 access goes through here."""

import sqlite3
import fnmatch
import random
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

        CREATE TABLE IF NOT EXISTS banwords (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel    TEXT NOT NULL,
            word       TEXT NOT NULL,
            added_by   TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel, word)
        );

        CREATE TABLE IF NOT EXISTS permbans (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel    TEXT NOT NULL,
            mask       TEXT NOT NULL,
            added_by   TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel, mask)
        );

        CREATE TABLE IF NOT EXISTS line_triggers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            hostmask   TEXT NOT NULL,
            channels   TEXT NOT NULL DEFAULT '*',
            mode       TEXT NOT NULL DEFAULT 'fixed' CHECK(mode IN ('fixed','random')),
            threshold  INTEGER,
            random_max INTEGER,
            messages   TEXT NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            added_by   TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS line_trigger_counts (
            trigger_id INTEGER NOT NULL,
            channel    TEXT NOT NULL,
            hostmask   TEXT NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            goal       INTEGER NOT NULL,
            PRIMARY KEY (trigger_id, channel, hostmask)
        );

        CREATE TABLE IF NOT EXISTS ai_watch (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            hostmask      TEXT NOT NULL,
            channels      TEXT NOT NULL DEFAULT '*',
            lines         INTEGER,
            system_prompt TEXT,
            enabled       INTEGER NOT NULL DEFAULT 1,
            added_by      TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # ai_watch predates the per-entry lines/system_prompt columns above — the
    # CREATE TABLE IF NOT EXISTS is a no-op against an existing older copy, so
    # add them here if missing rather than losing any rows already in it.
    ai_watch_cols = {r[1] for r in c.execute("PRAGMA table_info(ai_watch)")}
    if 'lines' not in ai_watch_cols:
        c.execute("ALTER TABLE ai_watch ADD COLUMN lines INTEGER")
    if 'system_prompt' not in ai_watch_cols:
        c.execute("ALTER TABLE ai_watch ADD COLUMN system_prompt TEXT")
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


# ── Banwords ────────────────────────────────────────────────────────────────

def banword_add(channel, word, added_by=''):
    _conn().execute('INSERT OR IGNORE INTO banwords(channel,word,added_by) VALUES(?,?,?)',
                    (channel.lower(), word.lower(), added_by))
    _conn().commit()


def banword_delete(channel, word):
    _conn().execute('DELETE FROM banwords WHERE channel=? AND word=?', (channel.lower(), word.lower()))
    _conn().commit()


def banword_list(channel):
    return [r['word'] for r in _conn().execute(
        'SELECT word FROM banwords WHERE channel=? ORDER BY word', (channel.lower(),))]


def banword_match(channel, text):
    """Return the first banned word/phrase found in text (case-insensitive substring), or None."""
    t = text.lower()
    rows = _conn().execute('SELECT word FROM banwords WHERE channel=?', (channel.lower(),)).fetchall()
    for r in rows:
        if r['word'] in t:
            return r['word']
    return None


# ── Permbans ────────────────────────────────────────────────────────────────

# Sentinel stored in permbans.channel to mean "every channel" rather than one
# specific channel. Chosen because it can never collide with a real channel
# name (those are validated/prefixed with '#' throughout the codebase).
GLOBAL_SCOPE = '*'


def permban_add(channel, mask, added_by=''):
    chan = channel if channel == GLOBAL_SCOPE else channel.lower()
    _conn().execute('INSERT OR IGNORE INTO permbans(channel,mask,added_by) VALUES(?,?,?)',
                    (chan, mask, added_by))
    _conn().commit()


def permban_delete(channel, mask):
    chan = channel if channel == GLOBAL_SCOPE else channel.lower()
    _conn().execute('DELETE FROM permbans WHERE channel=? AND mask=?', (chan, mask))
    _conn().commit()


def permban_delete_id(ban_id):
    _conn().execute('DELETE FROM permbans WHERE id=?', (ban_id,))
    _conn().commit()


def permban_set_scope(ban_id, channel):
    """Move an existing permban between global scope and a single channel."""
    chan = channel if channel == GLOBAL_SCOPE else channel.lower()
    _conn().execute('UPDATE OR IGNORE permbans SET channel=? WHERE id=?', (chan, ban_id))
    _conn().commit()


def permban_list(channel):
    """Masks that apply to this channel: entries scoped to it plus global entries."""
    chan = channel if channel == GLOBAL_SCOPE else channel.lower()
    return [r['mask'] for r in _conn().execute(
        'SELECT mask FROM permbans WHERE channel=? OR channel=? ORDER BY mask',
        (chan, GLOBAL_SCOPE))]


def permban_all():
    """Every permban row (for the web UI), regardless of scope."""
    return _conn().execute(
        'SELECT id,channel,mask,added_by,created_at FROM permbans ORDER BY channel,mask').fetchall()


def permban_match(channel, hostmask):
    """Return the permban mask that matches hostmask in this channel — checking
    both channel-specific entries and global (all-channel) entries — or None."""
    rows = _conn().execute('SELECT mask FROM permbans WHERE channel=? OR channel=?',
                           (channel.lower(), GLOBAL_SCOPE)).fetchall()
    for r in rows:
        if fnmatch.fnmatch(hostmask.lower(), r['mask'].lower()):
            return r['mask']
    return None


# ── Line-count triggers ──────────────────────────────────────────────────────
#
# An admin registers a hostmask pattern (nick!ident@host, fnmatch wildcards
# allowed) plus a channel or set of channels. Once a matching sender has sent
# `threshold` lines in a matching channel (or, in 'random' mode, a freshly
# rolled random number between 1 and `random_max`), the bot replies with one
# line picked at random out of `messages` (one candidate per line) and starts
# counting again from zero.
#
# Counting is per (trigger, channel, concrete-hostmask) so a wildcard pattern
# like '*!*@some.host' tracks each matching user independently, and a trigger
# scoped to several channels tracks each channel's line count separately.

ALL_CHANNELS = '*'  # channels field meaning "every channel the bot is in"


def linetrigger_add(hostmask, channels, mode, threshold, random_max, messages, added_by=''):
    channels = (channels or '').strip() or ALL_CHANNELS
    _conn().execute(
        '''INSERT INTO line_triggers(hostmask,channels,mode,threshold,random_max,messages,added_by)
           VALUES(?,?,?,?,?,?,?)''',
        (hostmask, channels, mode, threshold, random_max, messages, added_by))
    _conn().commit()


def linetrigger_delete(trigger_id):
    _conn().execute('DELETE FROM line_triggers WHERE id=?', (trigger_id,))
    _conn().execute('DELETE FROM line_trigger_counts WHERE trigger_id=?', (trigger_id,))
    _conn().commit()


def linetrigger_set_enabled(trigger_id, enabled):
    _conn().execute('UPDATE line_triggers SET enabled=? WHERE id=?', (1 if enabled else 0, trigger_id))
    _conn().commit()


def linetrigger_list():
    return _conn().execute(
        '''SELECT id,hostmask,channels,mode,threshold,random_max,messages,enabled,added_by,created_at
           FROM line_triggers ORDER BY id''').fetchall()


def linetrigger_enabled_for_channel(channel):
    """All enabled triggers whose channel scope includes `channel`."""
    channel = channel.lower()
    out = []
    for row in linetrigger_list():
        if not row['enabled']:
            continue
        scope = (row['channels'] or '').strip()
        if scope and scope != ALL_CHANNELS:
            chans = {c.strip().lower() for c in scope.split(',') if c.strip()}
            if channel not in chans:
                continue
        out.append(row)
    return out


def _linetrigger_goal(row):
    if row['mode'] == 'random':
        mx = max(1, int(row['random_max'] or 1))
        return random.randint(1, mx)
    return max(1, int(row['threshold'] or 1))


def linetrigger_bump(row, channel, hostmask):
    """Record one matching line for this trigger/channel/user. If the running
    count reaches the current goal, reset the counter (rolling a new goal for
    'random' mode) and return a single message picked at random from `messages`
    (one candidate per line), wrapped in a list; otherwise None."""
    tid  = row['id']
    chan = channel.lower()
    hm   = hostmask.lower()
    existing = _conn().execute(
        'SELECT count, goal FROM line_trigger_counts WHERE trigger_id=? AND channel=? AND hostmask=?',
        (tid, chan, hm)).fetchone()
    if existing is None:
        count = 1
        goal  = _linetrigger_goal(row)
        _conn().execute(
            'INSERT INTO line_trigger_counts(trigger_id,channel,hostmask,count,goal) VALUES(?,?,?,?,?)',
            (tid, chan, hm, count, goal))
    else:
        count = existing['count'] + 1
        goal  = existing['goal']
        _conn().execute(
            'UPDATE line_trigger_counts SET count=? WHERE trigger_id=? AND channel=? AND hostmask=?',
            (count, tid, chan, hm))
    _conn().commit()

    if count >= goal:
        new_goal = _linetrigger_goal(row)
        _conn().execute(
            'UPDATE line_trigger_counts SET count=0, goal=? WHERE trigger_id=? AND channel=? AND hostmask=?',
            (new_goal, tid, chan, hm))
        _conn().commit()
        lines = [line for line in row['messages'].split('\n') if line.strip()]
        return [random.choice(lines)] if lines else None
    return None


# ── AI watchlist (which hostmasks the Ollama auto-trigger responds to) ──────

def ai_watch_add(hostmask, channels, lines, system_prompt, added_by=''):
    channels = (channels or '').strip() or ALL_CHANNELS
    _conn().execute(
        '''INSERT INTO ai_watch(hostmask,channels,lines,system_prompt,added_by)
           VALUES(?,?,?,?,?)''',
        (hostmask, channels, lines, system_prompt, added_by))
    _conn().commit()


def ai_watch_update(watch_id, lines, system_prompt):
    _conn().execute(
        'UPDATE ai_watch SET lines=?, system_prompt=? WHERE id=?',
        (lines, system_prompt, watch_id))
    _conn().commit()


def ai_watch_delete(watch_id):
    _conn().execute('DELETE FROM ai_watch WHERE id=?', (watch_id,))
    _conn().commit()


def ai_watch_set_enabled(watch_id, enabled):
    _conn().execute('UPDATE ai_watch SET enabled=? WHERE id=?', (1 if enabled else 0, watch_id))
    _conn().commit()


def ai_watch_list():
    return _conn().execute(
        '''SELECT id,hostmask,channels,lines,system_prompt,enabled,added_by,created_at
           FROM ai_watch ORDER BY id''').fetchall()


def ai_watch_enabled_for_channel(channel):
    """All enabled ai_watch entries whose channel scope includes `channel`."""
    channel = channel.lower()
    out = []
    for row in ai_watch_list():
        if not row['enabled']:
            continue
        scope = (row['channels'] or '').strip()
        if scope and scope != ALL_CHANNELS:
            chans = {c.strip().lower() for c in scope.split(',') if c.strip()}
            if channel not in chans:
                continue
        out.append(row)
    return out


# ── Arbitrary query (admin command) ─────────────────────────────────────────

def run_query(sql):
    """Execute arbitrary SQL and return (columns, rows).  SELECT only for safety."""
    c = _conn()
    cur = c.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, [list(r) for r in rows]
