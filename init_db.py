#!/usr/bin/env python3
"""
Populate the database with factory defaults.
Safe to re-run — uses INSERT OR IGNORE / INSERT OR REPLACE only where needed.

All values are read from the environment so nothing site-specific (or
secret) is hardcoded here — see install.sh, which prompts for these and
exports them before calling this script.
"""

import os
import sqlite3
import sys
sys.path.insert(0, '/opt/stegobot')
import db

db.init_schema()

# ── Config defaults ──────────────────────────────────────────────────────────

DEFAULTS = {
    'nick':         os.environ.get('STEGOBOT_NICK', 'steg0saur'),
    'altNick':      os.environ.get('STEGOBOT_ALTNICK', 'stegOsaur'),
    'username':     os.environ.get('STEGOBOT_USERNAME', 'stegosaur'),
    'realname':     os.environ.get('STEGOBOT_REALNAME', 'stegosaur'),
    'ctcp_version': os.environ.get('STEGOBOT_CTCP_VERSION', 'irssi v1.4.3 - https://irssi.org'),
    'web_port':     os.environ.get('STEGOBOT_WEB_PORT', '8080'),
    'admin_email':  os.environ.get('STEGOBOT_ADMIN_EMAIL', ''),
    # SMTP — no defaults; leave unset until configured (install prompt or web config page)
    'smtp_host':    os.environ.get('STEGOBOT_SMTP_HOST', ''),
    'smtp_user':    os.environ.get('STEGOBOT_SMTP_USER', ''),
    'smtp_pass':    os.environ.get('STEGOBOT_SMTP_PASS', ''),
    'smtp_from':    os.environ.get('STEGOBOT_SMTP_FROM', ''),
}
# Drop empty values so they don't shadow a value set later via the web config page
DEFAULTS = {k: v for k, v in DEFAULTS.items() if v}

c = sqlite3.connect(db.DB_PATH)
for k, v in DEFAULTS.items():
    c.execute('INSERT OR IGNORE INTO config(key,value) VALUES(?,?)', (k, v))

# ── Default admin user ───────────────────────────────────────────────────────

admin_hostmask = os.environ.get('STEGOBOT_ADMIN_HOSTMASK', '')
if admin_hostmask:
    c.execute("INSERT OR IGNORE INTO users(hostmask,level) VALUES(?,'admin')", (admin_hostmask,))

# ── Default channels ─────────────────────────────────────────────────────────

channels = [ch.strip() for ch in os.environ.get('STEGOBOT_CHANNELS', '').split(',') if ch.strip()]
for ch in channels:
    c.execute("INSERT OR IGNORE INTO channels(name) VALUES(?)", (ch,))

# ── Default server list (priority 0 = first tried) ──────────────────────────

servers = [s.strip() for s in os.environ.get('STEGOBOT_SERVERS', '').split(',') if s.strip()]
for pri, entry in enumerate(servers):
    host, _, port = entry.partition(':')
    c.execute("INSERT OR IGNORE INTO servers(host,port,priority) VALUES(?,?,?)",
              (host, int(port) if port else 6667, pri))

c.commit()
c.close()
print('Database initialised at', db.DB_PATH)
