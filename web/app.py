#!/usr/bin/env python3
"""Flask web app — config dashboard + IRC web terminal."""

import gzip
import os
import re
import secrets
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from datetime import datetime
from pathlib import Path

from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, abort)
from flask_socketio import SocketIO, emit, join_room

sys.path.insert(0, '/opt/stegobot')
import db
import state

socketio = SocketIO(async_mode='threading')

LOG_DIR = Path('/opt/stegobot/logs')
_HM_RE  = re.compile(r'\(([^)]+![^)]+@[^)]+)\)')
_KINDS  = ('join|privmsg|part|quit|kick|mode|topic|notice|action'
           '|ctcp|connect|disconnect|motd|server|error|whois|names')


def _extract_hostmask(text):
    m = _HM_RE.search(text)
    return m.group(1) if m else ''


def _parse_log_line(line, channel):
    """Parse either log format into an event dict compatible with formatEvent."""
    line = line.strip()
    if not line:
        return None

    # New format: [YYYY-MM-DDTHH:MM:SS.ffffff] type nick text
    m = re.match(r'^\[(\d{4}-\d{2}-\d{2}T[\d:.]+)\] (\w+) (\S*) (.*)$', line)
    if m:
        ts, kind, nick, text = m.groups()
        nick = nick if nick not in ('', 'None') else ''
        hm = _extract_hostmask(text) if kind in ('join', 'part', 'quit') else ''
        return {'timestamp': ts, 'nick': nick, 'text': text,
                'type': kind, 'channel': channel, 'hostmask': hm}

    # Old format: [HH:MM:SS] {type}{nick}{type} {text}  (fields concatenated, no date)
    m = re.match(r'^\[(\d{2}:\d{2}:\d{2})\] (.*)$', line)
    if m:
        ts_short, rest = m.groups()
        today = datetime.utcnow().strftime('%Y-%m-%d')
        ts    = f'{today}T{ts_short}'
        tm = re.match(rf'^({_KINDS})(\S+?)\1 (.*)$', rest)
        if tm:
            kind, nick, text = tm.groups()
            hm = _extract_hostmask(text) if kind in ('join', 'part', 'quit') else ''
            return {'timestamp': ts, 'nick': nick, 'text': text,
                    'type': kind, 'channel': channel, 'hostmask': hm}
        # Fallback: unrecognised old-format line — show as-is
        return {'timestamp': ts, 'nick': '', 'text': rest,
                'type': 'server', 'channel': channel, 'hostmask': ''}

    return None


def _log_file_history(channel, limit=1000):
    """Load history from the most recent plain log + most recent gz for a channel."""
    safe = channel.lstrip('#').replace('/', '_').lower()
    all_lines = []

    log_files = sorted(LOG_DIR.glob(f'{safe}_*.log'))
    if log_files:
        try:
            with open(log_files[-1], 'r', encoding='utf-8', errors='replace') as f:
                all_lines.extend(f.readlines())
        except Exception:
            pass

    gz_files = sorted(LOG_DIR.glob(f'{safe}_*.log.gz'))
    if gz_files:
        try:
            with gzip.open(gz_files[-1], 'rt', encoding='utf-8', errors='replace') as f:
                all_lines.extend(f.readlines())
        except Exception:
            pass

    events = []
    for line in all_lines:
        e = _parse_log_line(line, channel)
        if e:
            events.append(e)

    events.sort(key=lambda e: e['timestamp'])
    return events[-limit:]


def create_app():
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.secret_key = db.cfg_get('web_secret', secrets.token_hex(32))
    if not db.cfg_get('web_secret'):
        db.cfg_set('web_secret', app.secret_key)
    socketio.init_app(app, cors_allowed_origins='*')
    _register_routes(app)
    return app


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('email'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def _send_magic_link(to_email, link):
    host = db.cfg_get('smtp_host', 'email-smtp.us-east-1.amazonaws.com')
    user = db.cfg_get('smtp_user', '')
    pwd  = db.cfg_get('smtp_pass', '')
    frm  = db.cfg_get('smtp_from', 'namssa@gmail.com')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = 'StegoBot login link'
    msg['From']    = frm
    msg['To']      = to_email
    body = f"""<p>Click to log in to StegoBot (link expires after one use):</p>
<p><a href="{link}">{link}</a></p>"""
    msg.attach(MIMEText(body, 'html'))

    with smtplib.SMTP(host, 587) as s:
        s.starttls()
        s.login(user, pwd)
        s.sendmail(frm, to_email, msg.as_string())


def _register_routes(app):

    @app.route('/')
    @login_required
    def index():
        return redirect(url_for('config_page'))

    # ── Auth ───────────────────────────────────────────────────────────────

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        msg = None
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            admin_email = db.cfg_get('admin_email', 'namssa@gmail.com').lower()
            if email == admin_email:
                token = secrets.token_urlsafe(32)
                db.session_create(token, email)
                try:
                    link = url_for('auth', token=token, _external=True)
                    _send_magic_link(email, link)
                    msg = 'Login link sent — check your email.'
                except Exception as exc:
                    msg = f'Could not send email: {exc}'
            else:
                # Don't reveal whether the email exists
                msg = 'If that email is registered, a link has been sent.'
        return render_template('login.html', msg=msg)

    @app.route('/auth/<token>')
    def auth(token):
        email = db.session_consume(token)
        if email:
            session['email'] = email
            return redirect(url_for('index'))
        return 'Invalid or expired link.', 400

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    # ── Config page ────────────────────────────────────────────────────────

    @app.route('/config', methods=['GET', 'POST'])
    @login_required
    def config_page():
        msg = None
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'set':
                key   = request.form.get('key', '').strip()
                value = request.form.get('value', '').strip()
                if key:
                    db.cfg_set(key, value)
                    msg = f'Set {key}'
            elif action == 'delete':
                key = request.form.get('key', '').strip()
                db.cfg_delete(key)
                msg = f'Deleted {key}'

        config   = db.cfg_all()
        users    = db.user_list()
        channels = db.chan_all()
        servers  = db.srv_list()
        return render_template('config.html',
                               config=config, users=users,
                               channels=channels, servers=servers, msg=msg)

    @app.route('/config/user/add', methods=['POST'])
    @login_required
    def add_user():
        hm    = request.form.get('hostmask', '').strip()
        level = request.form.get('level', 'peon')
        if hm and level in ('peon', 'admin'):
            db.user_add(hm, level)
        return redirect(url_for('config_page'))

    @app.route('/config/user/del', methods=['POST'])
    @login_required
    def del_user():
        hm = request.form.get('hostmask', '').strip()
        if hm:
            db.user_delete(hm)
        return redirect(url_for('config_page'))

    @app.route('/config/server/add', methods=['POST'])
    @login_required
    def add_server():
        host = request.form.get('host', '').strip()
        port = int(request.form.get('port', 6667))
        if host:
            db.srv_add(host, port)
        return redirect(url_for('config_page'))

    @app.route('/config/server/del', methods=['POST'])
    @login_required
    def del_server():
        host = request.form.get('host', '').strip()
        if host:
            db.srv_delete(host)
        return redirect(url_for('config_page'))

    @app.route('/config/channel/add', methods=['POST'])
    @login_required
    def add_channel():
        name = request.form.get('name', '').strip().lower()
        if name:
            chan = name if name.startswith('#') else f'#{name}'
            db.chan_add(chan)
            bot = state.bot_instance
            if bot and bot._conn and bot._conn.is_connected():
                bot._conn.join(chan)
        return redirect(url_for('config_page'))

    @app.route('/config/channel/del', methods=['POST'])
    @login_required
    def del_channel():
        name = request.form.get('name', '').strip()
        if name:
            db.chan_remove(name)
            bot = state.bot_instance
            if bot and bot._conn and bot._conn.is_connected():
                bot._conn.part(name)
        return redirect(url_for('config_page'))

    # ── Debug ─────────────────────────────────────────────────────────────

    @app.route('/debug/history/<path:channel>')
    @login_required
    def debug_history(channel):
        if not channel.startswith('#'):
            channel = '#' + channel
        channel = channel.lower()
        history = _log_file_history(channel, limit=20)
        return jsonify({'channel': channel, 'count': len(history), 'first': history[:3], 'last': history[-3:]})

    # ── Terminal page ──────────────────────────────────────────────────────

    @app.route('/terminal')
    @login_required
    def terminal():
        channels = db.chan_list()
        active   = request.args.get('chan', channels[0] if channels else '#fmc')
        history  = list(reversed(list(db.log_recent(active, 100))))
        return render_template('terminal.html',
                               channels=channels, active=active, history=history)

    # ── SocketIO events ────────────────────────────────────────────────────

    @socketio.on('connect')
    def on_connect():
        pass  # auth checked via session cookie at HTTP level

    @socketio.on('subscribe')
    def on_subscribe(data):
        import traceback, logging as _log
        channel = data.get('channel', '').lower()
        join_room(channel)
        try:
            history = _log_file_history(channel, limit=1000)
            _log.getLogger('stegobot').info('subscribe %s → %d events', channel, len(history))
        except Exception:
            _log.getLogger('stegobot').error('history error for %s:\n%s', channel, traceback.format_exc())
            history = []
        emit('history', {'channel': channel, 'lines': history})

        # Current names from bot's live tracking
        bot = state.bot_instance
        if bot and channel.lower() in bot.channel_users:
            users = sorted(
                [{'nick': v['nick'], 'prefix': v.get('prefix', ''),
                  'hostmask': v.get('hostmask', '')}
                 for v in bot.channel_users[channel.lower()].values()],
                key=lambda u: (0 if '@' in u['prefix'] else 1 if '+' in u['prefix'] else 2,
                               u['nick'].lower())
            )
            emit('irc_event', {'type': 'names_update', 'channel': channel,
                               'users': users,
                               'timestamp': datetime.utcnow().isoformat()})

    @socketio.on('send_message')
    def on_send_message(data):
        channel = data.get('channel', '').lower()
        text    = data.get('text', '').strip()
        if not text or not channel:
            return
        bot = state.bot_instance
        if not bot or not bot._conn or not bot._conn.is_connected():
            emit('irc_event', {'type': 'error', 'channel': channel,
                               'text': 'Bot not connected.', 'nick': '',
                               'timestamp': datetime.utcnow().isoformat()})
            return
        if text.startswith('/'):
            _handle_terminal_command(bot, text, channel)
        else:
            bot._conn.privmsg(channel, text)
            entry = {
                'channel': channel, 'nick': bot.get_nick(),
                'text': text, 'type': 'privmsg',
                'timestamp': datetime.utcnow().isoformat()
            }
            state.buffer_push(channel, entry)
            db.log_msg(channel, bot.get_nick(), text, 'privmsg')

    @socketio.on('irc_action')
    def on_irc_action(data):
        """Web-initiated IRC actions: kick, ban, op, deop, voice, devoice, mode, whois, topic."""
        bot = state.bot_instance
        if not bot or not bot._conn or not bot._conn.is_connected():
            return
        action  = data.get('action', '')
        channel = data.get('channel', '').lower()
        nick    = data.get('nick', '')
        reason  = data.get('reason', '')
        if action == 'kick':
            bot.web_kick(channel, nick, reason)
        elif action == 'kickban':
            bot.web_ban(channel, nick)
            bot.web_kick(channel, nick, reason)
        elif action == 'ban':
            bot.web_ban(channel, nick)
        elif action == 'unban':
            hm = bot.hostmask_cache.get(nick.lower(), f'{nick}!*@*')
            _, _, host = hm.partition('@')
            bot.web_mode(channel, '-b', f'*!*@{host}')
        elif action == 'op':
            bot.web_mode(channel, '+o', nick)
        elif action == 'deop':
            bot.web_mode(channel, '-o', nick)
        elif action == 'voice':
            bot.web_mode(channel, '+v', nick)
        elif action == 'devoice':
            bot.web_mode(channel, '-v', nick)
        elif action == 'whois':
            bot.web_whois(nick, channel)
        elif action == 'topic':
            bot.web_topic(channel, data.get('topic', ''))


def _handle_terminal_command(bot, text, default_channel):
    raw   = text.lstrip('/')
    parts = raw.split(None, 2)
    if not parts:
        return
    cmd = parts[0].lower()

    if cmd == 'join' and len(parts) >= 2:
        raw_chan = parts[1] if parts[1].startswith('#') else f'#{parts[1]}'
        chan = raw_chan.lower()
        bot._conn.join(chan)
        db.chan_add(chan)
    elif cmd in ('part', 'leave'):
        raw_chan = parts[1] if len(parts) >= 2 else default_channel
        chan = (raw_chan if raw_chan.startswith('#') else f'#{raw_chan}').lower()
        bot._conn.part(chan)
        db.chan_remove(chan)
    elif cmd == 'nick' and len(parts) >= 2:
        bot._conn.nick(parts[1])
        bot.nick = parts[1]
        db.cfg_set('nick', parts[1])
    elif cmd == 'msg' and len(parts) >= 3:
        bot._conn.privmsg(parts[1], parts[2])
    elif cmd == 'me' and len(parts) >= 2:
        action_text = ' '.join(parts[1:])
        bot._conn.action(default_channel, action_text)
        state.buffer_push(default_channel, {
            'type': 'action', 'nick': bot.get_nick(), 'hostmask': '',
            'text': action_text, 'channel': default_channel,
            'timestamp': datetime.utcnow().isoformat()
        })
    elif cmd == 'quit':
        bot._conn.quit(parts[1] if len(parts) >= 2 else 'Quit')
    elif cmd == 'kick' and len(parts) >= 2:
        target_parts = parts[1].split(None, 1)
        nick   = target_parts[0]
        reason = target_parts[1] if len(target_parts) > 1 else ''
        bot.web_kick(default_channel, nick, reason)
    elif cmd == 'ban' and len(parts) >= 2:
        bot.web_ban(default_channel, parts[1])
    elif cmd == 'unban' and len(parts) >= 2:
        bot.web_mode(default_channel, '-b', parts[1])
    elif cmd == 'op' and len(parts) >= 2:
        bot.web_mode(default_channel, '+o', parts[1])
    elif cmd == 'deop' and len(parts) >= 2:
        bot.web_mode(default_channel, '-o', parts[1])
    elif cmd == 'voice' and len(parts) >= 2:
        bot.web_mode(default_channel, '+v', parts[1])
    elif cmd == 'devoice' and len(parts) >= 2:
        bot.web_mode(default_channel, '-v', parts[1])
    elif cmd == 'topic' and len(parts) >= 2:
        bot.web_topic(default_channel, parts[1])
    elif cmd == 'whois' and len(parts) >= 2:
        bot.web_whois(parts[1], default_channel)
    elif cmd == 'mode' and len(parts) >= 2:
        if parts[1].startswith('#'):
            target = parts[1].lower()
            rest   = parts[2] if len(parts) >= 3 else ''
        else:
            target = default_channel
            rest   = ' '.join(parts[1:])
        mode_parts = rest.split(None, 1) if rest else []
        bot.web_mode(target, mode_parts[0] if mode_parts else '', mode_parts[1] if len(mode_parts) > 1 else '')
    elif cmd == 'raw' and len(parts) >= 2:
        bot._conn.send_raw(parts[1] if len(parts) >= 2 else '')
    else:
        # Unknown command — send as raw IRC (handles /stats /links /time /version etc.)
        bot._conn.send_raw(raw)
