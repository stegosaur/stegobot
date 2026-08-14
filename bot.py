#!/usr/bin/env python3
"""StegoBot IRC core — full event tracking, web bridge."""

import gzip
import irc.client
import logging
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import db
import plugin_loader
import state

LOG_DIR = Path('/opt/stegobot/logs')
LOG_DIR.mkdir(exist_ok=True)
logger = logging.getLogger('stegobot')


# ── Channel file logger ───────────────────────────────────────────────────────

class ChannelLog:
    """Open, append, close on every write rather than caching a handle per channel.

    The previous version cached one open file handle per channel forever (only
    closed on midnight rollover). Every distinct query window is its own
    "channel" now (see _on_privmsg), so anyone who ever DMs the bot would pin
    an open fd for the rest of the process's life — that's exactly what
    exhausted the process's file descriptor limit and killed the bot before
    (`OSError: Too many open files`). Log write volume here is far too low for
    open+append+close per line to matter, and it makes fd exhaustion
    structurally impossible instead of just less likely.
    """
    _lock = threading.Lock()
    _last_date = {}

    @classmethod
    def write(cls, channel, line):
        safe = channel.lstrip('#').replace('/', '_').lower()
        today = datetime.utcnow().strftime('%Y-%m-%d')
        with cls._lock:
            prev = cls._last_date.get(safe)
            if prev is not None and prev != today:
                # Real date rollover — compress yesterday's log
                cls._rotate(safe, prev)
            cls._last_date[safe] = today
            path = LOG_DIR / f'{safe}_{today}.log'
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')

    @classmethod
    def _rotate(cls, safe, date_str):
        """Compress the log for date_str."""
        p = LOG_DIR / f'{safe}_{date_str}.log'
        if p.exists():
            gz = p.with_suffix('.log.gz')
            if not gz.exists():
                with open(p, 'rb') as fi, gzip.open(gz, 'wb') as fo:
                    shutil.copyfileobj(fi, fo)
            p.unlink()


def _push(channel, entry):
    """Push an event dict to the web buffer and log file."""
    ts   = entry.get('timestamp', datetime.utcnow().isoformat())
    nick = entry.get('nick') or ''
    text = entry.get('text') or ''
    ChannelLog.write(channel, f"[{ts}] {entry.get('type','?')} {nick} {text}")
    state.buffer_push(channel, entry)


def _now():
    return datetime.utcnow().isoformat()


# ── Bot ───────────────────────────────────────────────────────────────────────

class StegoBot:

    def __init__(self):
        self.reactor = irc.client.Reactor()
        self._conn = None
        self.nick = ''
        self.should_stop = False
        self.reconnect_requested = None  # (host, port)
        self._manual_disconnect = False  # True while a /disconnect should not auto-reconnect

        # LIST accumulator (322 rows collected between 321 liststart / 323 listend)
        self.list_acc = []

        # User tracking: channel -> {nick_lower: {nick, prefix, hostmask}}
        self.channel_users = defaultdict(dict)
        # Hostmask cache from JOIN / WHO: nick_lower -> hostmask
        self.hostmask_cache = {}

        # WHOIS accumulator: nick_lower -> dict
        self.whois_acc = {}
        # WHOIS web callbacks: nick_lower -> reply_target
        self.whois_web = {}
        # Pending adduser callbacks: nick_lower -> (cb, args)
        self.whois_pending = {}

        self._config_ts = 0
        self._motd_buf = []

        handlers = [
            ('welcome',       self._on_welcome),
            ('pubmsg',        self._on_pubmsg),
            ('privmsg',       self._on_privmsg),
            ('action',        self._on_action),
            ('ctcp',          self._on_ctcp),
            ('join',          self._on_join),
            ('part',          self._on_part),
            ('quit',          self._on_quit),
            ('kick',          self._on_kick),
            ('nick',          self._on_nick_change),
            ('mode',          self._on_mode),
            ('topic',         self._on_topic),
            ('currenttopic',  self._on_current_topic),
            ('namreply',      self._on_namreply),
            ('whoreply',      self._on_whoreply),
            ('endofwho',      self._on_endofwho),
            ('whoisuser',     self._on_whoisuser),
            ('whoisserver',   self._on_whoisserver),
            ('whoischannels', self._on_whoischannels),
            ('whoisidle',     self._on_whoisidle),
            ('endofwhois',    self._on_endofwhois),
            ('motd',          self._on_motd),
            ('motdstart',     self._on_motdstart),
            ('endofmotd',     self._on_endofmotd),
            ('notice',        self._on_notice),
            ('privnotice',    self._on_notice),
            ('nicknameinuse',      self._on_nick_in_use),
            ('cannotsendtochan',   self._on_cannotsendtochan),
            ('chanoprivsneeded',   self._on_chanoprivsneeded),
            ('banlist',            self._on_banlist),
            ('endofbanlist',       self._on_endofbanlist),
            ('channelmodeis',      self._on_channelmodeis),
            ('exceptlist',         self._on_exceptlist),
            ('endofexceptlist',    self._on_endofexceptlist),
            ('invitelist',         self._on_invitelist),
            ('endofinvitelist',    self._on_endofinvitelist),
            ('liststart',          self._on_liststart),
            ('list',               self._on_list),
            ('listend',            self._on_listend),
            ('disconnect',         self._on_disconnect),
            ('error',              self._on_error),
            ('all_raw_messages',   self._on_raw_numeric),
        ]
        for evt, fn in handlers:
            self.reactor.add_global_handler(evt, fn)

        # Numerics we already handle — don't forward to *status* again
        self._handled_numerics = {
            '001','002','003','004','005',        # welcome burst
            '332','333',                          # topic / topic set-by
            '353','366',                          # NAMES reply + end
            '352','315',                          # WHO reply + end
            '311','312','313','317','318','319',  # WHOIS
            '330',                                # WHOIS account
            '372','375','376',                    # MOTD
            '324',                                # channelmodeis
            '346','347',                          # invitelist / endofinvitelist
            '348','349',                          # exceptlist / endofexceptlist
            '367','368',                          # banlist / endofbanlist
            '321','322','323',                    # LIST — handled, rendered as a table window
            '433',                                # nick in use
            '404',                                # cannotsendtochan
            '482',                                # chanoprivsneeded
            '421',                                # unknown command (swallow)
        }

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, host=None, port=None):
        if host is None:
            host, port = db.srv_next()
        if port is None:
            port = 6667
        self._manual_disconnect = False
        self.nick     = db.cfg_get('nick', 'stegobot')
        username      = db.cfg_get('username', 'stegobot')
        realname      = db.cfg_get('realname', 'stegobot')
        logger.info('Connecting to %s:%s as %s!%s', host, port, self.nick, username)
        db.srv_rotate(host)
        _push('*status*', {'type': 'connect', 'nick': '', 'text': f'Connecting to {host}:{port}…', 'timestamp': _now()})
        try:
            srv = self.reactor.server()
            srv.connect(host, int(port), self.nick, username=username, ircname=realname)
            self._conn = srv
        except irc.client.ServerConnectionError as exc:
            logger.error('Connection failed: %s', exc)
            _push('*status*', {'type': 'error', 'nick': '', 'text': f'Connection failed: {exc}', 'timestamp': _now()})
            self._try_next()

    def _try_next(self):
        time.sleep(10)
        h, p = db.srv_next()
        if h:
            self.connect(h, p)

    def disconnect(self, msg='Changing servers'):
        if self._conn and self._conn.is_connected():
            self._conn.quit(msg)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while not self.should_stop:
            try:
                self.reactor.process_once(timeout=0.2)
            except Exception as exc:
                logger.exception('Reactor: %s', exc)

            for target, text in state.drain_send_queue():
                self._safe_privmsg(target, text)

            if self.reconnect_requested:
                host, port = self.reconnect_requested
                self.reconnect_requested = None
                self.disconnect('Server change requested')
                time.sleep(2)
                self.connect(host, port)

    def _safe_privmsg(self, target, text):
        if self._conn and self._conn.is_connected():
            self._conn.privmsg(target, text)

    def get_nick(self):
        return self.nick

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _mask(self, source):
        try:
            nm = irc.client.NickMask(str(source))
            hm = f'{nm.nick}!{nm.user}@{nm.host}'
            self.hostmask_cache[nm.nick.lower()] = hm
            return nm.nick, hm
        except Exception:
            return str(source), str(source)

    def _prefix_of(self, channel, nick_lower):
        u = self.channel_users[channel.lower()].get(nick_lower, {})
        return u.get('prefix', '')

    def _update_prefix(self, channel, nick, prefix, add=True):
        ch = channel.lower()
        nl = nick.lower()
        if nl in self.channel_users[ch]:
            cur = self.channel_users[ch][nl].get('prefix', '')
            if add:
                if prefix not in cur:
                    self.channel_users[ch][nl]['prefix'] = prefix + cur
            else:
                self.channel_users[ch][nl]['prefix'] = cur.replace(prefix, '')

    def _emit_names(self, channel):
        ch = channel.lower()
        users = [
            {'nick': v['nick'], 'prefix': v.get('prefix', ''), 'hostmask': v.get('hostmask', '')}
            for v in sorted(self.channel_users[ch].values(),
                            key=lambda u: (0 if '@' in u.get('prefix','') else
                                           1 if '+' in u.get('prefix','') else 2,
                                           u['nick'].lower()))
        ]
        state.buffer_push(channel, {'type': 'names_update', 'channel': channel, 'users': users, 'timestamp': _now()})

    # ── IRC event handlers ────────────────────────────────────────────────────

    def _on_welcome(self, c, e):
        server = e.source or 'server'
        _push('*status*', {'type': 'connect', 'nick': '', 'text': f'Connected to {server}', 'timestamp': _now()})
        for chan in db.chan_list():
            time.sleep(0.3)
            c.join(chan)

    def _on_pubmsg(self, c, e):
        nick, hm = self._mask(e.source)
        channel  = e.target.lower()
        text     = e.arguments[0]
        _push(channel, {'type': 'privmsg', 'nick': nick, 'hostmask': hm, 'text': text,
                         'channel': channel, 'timestamp': _now()})

        prefix = self.nick + ':'
        if text.lower().startswith(prefix.lower()):
            self._dispatch(c, text[len(prefix):].strip(),
                           channel=channel, source=str(e.source), public=True)

    def _on_privmsg(self, c, e):
        nick, hm = self._mask(e.source)
        text     = e.arguments[0]
        # Keyed by the sender's nick (a query window per correspondent), matching
        # web_privmsg's outgoing target — previously this was a single shared
        # 'privmsg' bucket that merged every correspondent into one window.
        target   = nick.lower()
        _push(target, {'type': 'privmsg', 'nick': nick, 'hostmask': hm, 'text': text,
                        'channel': target, 'timestamp': _now()})
        self._dispatch(c, text, channel=None, source=str(e.source), public=False, reply_to=nick)

    def _on_action(self, c, e):
        nick, hm = self._mask(e.source)
        channel  = e.target.lower() if e.target != self.nick else 'privmsg'
        text     = e.arguments[0] if e.arguments else ''
        _push(channel, {'type': 'action', 'nick': nick, 'hostmask': hm, 'text': text,
                         'channel': channel, 'timestamp': _now()})

    def _on_ctcp(self, c, e):
        nick, hm = self._mask(e.source)
        cmd  = e.arguments[0].upper() if e.arguments else ''
        args = e.arguments[1] if len(e.arguments) > 1 else ''
        if cmd == 'VERSION':
            ver = db.cfg_get('ctcp_version', 'stegobot')
            c.ctcp_reply(nick, f'VERSION {ver}')
        _push('*status*', {'type': 'ctcp', 'nick': nick, 'hostmask': hm,
                            'text': f'CTCP {cmd} from {nick} ({hm}){": " + args if args else ""}',
                            'ctcp_cmd': cmd, 'ctcp_args': args, 'timestamp': _now()})

    def _on_join(self, c, e):
        nick, hm = self._mask(e.source)
        channel  = e.target.lower()
        self.channel_users[channel][nick.lower()] = {'nick': nick, 'prefix': '', 'hostmask': hm}
        _push(channel, {'type': 'join', 'nick': nick, 'hostmask': hm,
                         'channel': channel, 'text': f'{nick} ({hm}) has joined {channel}',
                         'timestamp': _now()})
        if nick == self.nick:
            db.chan_add(channel)
            c.who(channel)
        self._emit_names(channel)

    def _on_part(self, c, e):
        nick, hm = self._mask(e.source)
        channel  = e.target.lower()
        reason   = e.arguments[0] if e.arguments else ''
        if nick.lower() in self.channel_users[channel]:
            hm = self.channel_users[channel][nick.lower()].get('hostmask', hm)
            del self.channel_users[channel][nick.lower()]
        _push(channel, {'type': 'part', 'nick': nick, 'hostmask': hm,
                         'channel': channel, 'reason': reason,
                         'text': f'{nick} ({hm}) has left {channel} ({reason})',
                         'timestamp': _now()})
        if nick == self.nick:
            db.chan_remove(channel)
        self._emit_names(channel)

    def _on_quit(self, c, e):
        nick, hm = self._mask(e.source)
        reason   = e.arguments[0] if e.arguments else ''
        nl       = nick.lower()
        affected = []
        for ch, users in self.channel_users.items():
            if nl in users:
                hm = users[nl].get('hostmask', hm)
                del users[nl]
                affected.append(ch)
        entry = {'type': 'quit', 'nick': nick, 'hostmask': hm, 'reason': reason,
                 'text': f'{nick} ({hm}) has quit ({reason})', 'timestamp': _now()}
        for ch in affected:
            _push(ch, dict(entry, channel=ch))
            self._emit_names(ch)
        if not affected:
            _push('*status*', dict(entry, channel='*status*'))

    def _on_kick(self, c, e):
        kicker, km = self._mask(e.source)
        channel    = e.target.lower()
        kicked     = e.arguments[0]
        reason     = e.arguments[1] if len(e.arguments) > 1 else ''
        self.channel_users[channel].pop(kicked.lower(), None)
        _push(channel, {'type': 'kick', 'nick': kicker, 'hostmask': km,
                         'kicked': kicked, 'reason': reason, 'channel': channel,
                         'text': f'{kicked} was kicked by {kicker} ({reason})',
                         'timestamp': _now()})
        self._emit_names(channel)

    def _on_nick_change(self, c, e):
        old_nick, hm = self._mask(e.source)
        new_nick     = e.target
        old_l, new_l = old_nick.lower(), new_nick.lower()
        if old_nick == self.nick:
            self.nick = new_nick
            db.cfg_set('nick', new_nick)
        self.hostmask_cache[new_l] = hm.replace(old_nick + '!', new_nick + '!')
        affected = []
        for ch, users in self.channel_users.items():
            if old_l in users:
                data = users.pop(old_l)
                data['nick'] = new_nick
                users[new_l] = data
                affected.append(ch)
        entry = {'type': 'nick', 'nick': old_nick, 'new_nick': new_nick,
                 'hostmask': hm, 'text': f'{old_nick} is now known as {new_nick}',
                 'timestamp': _now()}
        for ch in affected:
            _push(ch, dict(entry, channel=ch))
            self._emit_names(ch)
        if not affected:
            _push('*status*', dict(entry, channel='*status*'))

    def _on_mode(self, c, e):
        setter, sm = self._mask(e.source)
        is_chan    = e.target.startswith('#')
        target     = e.target.lower() if is_chan else e.target
        mode_str   = ' '.join(e.arguments) if e.arguments else ''

        # Update prefix tracking for +o/-o +v/-v
        if e.arguments and is_chan:
            mode_chars = e.arguments[0]
            mode_args  = e.arguments[1:]
            add = True
            arg_idx = 0
            prefix_map = {'o': '@', 'h': '%', 'v': '+'}
            for ch in mode_chars:
                if ch == '+':
                    add = True
                elif ch == '-':
                    add = False
                elif ch in prefix_map and arg_idx < len(mode_args):
                    self._update_prefix(target, mode_args[arg_idx], prefix_map[ch], add)
                    arg_idx += 1
                elif ch in 'beIklLfjJ':
                    arg_idx += 1

        _push(target if is_chan else '*status*',
              {'type': 'mode', 'nick': setter, 'hostmask': sm,
               'channel': target, 'mode': mode_str,
               'text': f'Mode {target} [{mode_str}] by {setter}',
               'timestamp': _now()})
        if is_chan:
            self._emit_names(target)

    def _on_topic(self, c, e):
        nick, hm = self._mask(e.source)
        channel  = e.target.lower()
        topic    = e.arguments[0] if e.arguments else ''
        db.chan_set_topic(channel, topic)
        _push(channel, {'type': 'topic', 'nick': nick, 'hostmask': hm,
                         'channel': channel, 'topic': topic,
                         'text': f'{nick} changed topic to: {topic}', 'timestamp': _now()})
        state.buffer_push(channel, {'type': 'topic_update', 'channel': channel, 'topic': topic, 'timestamp': _now()})

    def _on_current_topic(self, c, e):
        channel = e.arguments[0].lower()
        topic   = e.arguments[1] if len(e.arguments) > 1 else ''
        db.chan_set_topic(channel, topic)
        state.buffer_push(channel, {'type': 'topic_update', 'channel': channel, 'topic': topic, 'timestamp': _now()})

    def _on_namreply(self, c, e):
        channel = e.arguments[1].lower()
        raw     = e.arguments[2]
        for entry in raw.split():
            prefix = ''
            nick   = entry
            if entry and entry[0] in '@%+':
                prefix = entry[0]
                nick   = entry[1:]
            nl = nick.lower()
            if nl not in self.channel_users[channel]:
                self.channel_users[channel][nl] = {'nick': nick, 'prefix': prefix, 'hostmask': ''}
            else:
                self.channel_users[channel][nl]['prefix'] = prefix
        db.chan_set_users(channel, len(self.channel_users[channel]))

    def _on_whoreply(self, c, e):
        # args: [channel, user, host, server, nick, status, hopcount_realname]
        if len(e.arguments) < 6:
            return
        channel = e.arguments[0].lower()
        user    = e.arguments[1]
        host    = e.arguments[2]
        nick    = e.arguments[4]
        status  = e.arguments[5]
        hm      = f'{nick}!{user}@{host}'
        nl      = nick.lower()
        self.hostmask_cache[nl] = hm
        prefix  = '@' if '@' in status else ('+' if '+' in status else '')
        if nl in self.channel_users[channel]:
            self.channel_users[channel][nl]['hostmask'] = hm
            if not self.channel_users[channel][nl].get('prefix'):
                self.channel_users[channel][nl]['prefix'] = prefix
        else:
            self.channel_users[channel][nl] = {'nick': nick, 'prefix': prefix, 'hostmask': hm}

    def _on_endofwho(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        if channel.startswith('#'):
            self._emit_names(channel)

    # ── WHOIS ─────────────────────────────────────────────────────────────────

    def _on_whoisuser(self, c, e):
        if len(e.arguments) < 4:
            return
        nick = e.arguments[0]
        self.whois_acc[nick.lower()] = {
            'nick': nick, 'user': e.arguments[1], 'host': e.arguments[2],
            'realname': e.arguments[4] if len(e.arguments) > 4 else '',
        }

    def _on_whoisserver(self, c, e):
        nick = e.arguments[0].lower()
        if nick in self.whois_acc:
            self.whois_acc[nick]['server'] = e.arguments[1] if len(e.arguments) > 1 else ''

    def _on_whoischannels(self, c, e):
        nick = e.arguments[0].lower()
        if nick in self.whois_acc:
            self.whois_acc[nick]['channels'] = e.arguments[1] if len(e.arguments) > 1 else ''

    def _on_whoisidle(self, c, e):
        nick = e.arguments[0].lower()
        if nick in self.whois_acc:
            self.whois_acc[nick]['idle'] = e.arguments[1] if len(e.arguments) > 1 else ''

    def _on_endofwhois(self, c, e):
        nick = e.arguments[0].lower() if e.arguments else ''
        data = self.whois_acc.pop(nick, None)

        # adduser callback
        pending = self.whois_pending.pop(nick, None)
        if pending and data:
            hm = f"*!{data['user']}@{data['host']}"
            pending['cb'](hm, *pending['args'])

        # web /whois request
        reply_target = self.whois_web.pop(nick, None)
        if data:
            target = reply_target or '*status*'
            hm = f"{data['nick']}!{data.get('user','')}@{data.get('host','')}"
            self.hostmask_cache[nick] = hm
            state.buffer_push(target, {
                'type': 'whois', 'nick': data['nick'],
                'whois': data, 'timestamp': _now(),
                'text': f"WHOIS {data['nick']}"
            })

    # ── MOTD ──────────────────────────────────────────────────────────────────

    def _on_motdstart(self, c, e):
        self._motd_buf = []

    def _on_motd(self, c, e):
        line = e.arguments[0] if e.arguments else ''
        self._motd_buf.append(line)
        _push('*status*', {'type': 'motd', 'nick': '', 'text': line, 'timestamp': _now()})

    def _on_endofmotd(self, c, e):
        _push('*status*', {'type': 'motd', 'nick': '', 'text': '-- End of MOTD --', 'timestamp': _now()})

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _on_notice(self, c, e):
        nick, hm = self._mask(e.source)
        text     = e.arguments[0] if e.arguments else ''
        _push('*status*', {'type': 'notice', 'nick': nick, 'hostmask': hm,
                            'text': text, 'timestamp': _now()})

    def _on_nick_in_use(self, c, e):
        requested = e.arguments[0] if e.arguments else '?'
        alt = db.cfg_get('altNick', 'steg0bot')
        if self.nick != alt:
            self.nick = alt
            c.nick(alt)
            _push('*status*', {'type': 'error', 'nick': '', 'timestamp': _now(),
                                'text': f'Nick {requested} in use, trying {alt}'})
        else:
            _push('*status*', {'type': 'error', 'nick': '', 'timestamp': _now(),
                                'text': f'Nick {requested} is already in use'})

    def _on_disconnect(self, c, e):
        reason = e.arguments[0] if e.arguments else ''
        logger.warning('Disconnected: %s', reason)
        _push('*status*', {'type': 'disconnect', 'nick': '', 'text': f'Disconnected: {reason}', 'timestamp': _now()})
        if not self.should_stop and not self._manual_disconnect:
            time.sleep(15)
            self._try_next()

    def _on_error(self, c, e):
        text = e.arguments[0] if e.arguments else str(e)
        _push('*status*', {'type': 'error', 'nick': '', 'text': text, 'timestamp': _now()})

    # ── Command dispatch ──────────────────────────────────────────────────────

    def _dispatch(self, c, text, *, channel, source, public, reply_to=None):
        mask  = str(source)
        level = db.user_level(mask)
        reply = channel if public else reply_to
        parts = text.strip().split(None, 2)
        if not parts:
            return
        cmd  = parts[0].lower()
        args = text.strip()[len(parts[0]):].strip()

        # Plugin commands — checked before auth gate so PUBLIC plugins work for all users
        ctx = {
            'nick':    irc.client.NickMask(mask).nick,
            'channel': channel,
            'public':  public,
            'level':   level,
            'reply':   (lambda msg: c.privmsg(reply, msg)) if reply else (lambda msg: None),
            'conn':    c,
        }
        if plugin_loader.dispatch(cmd, args, ctx):
            return

        if level is None:
            return

        if cmd == 'op' and len(parts) >= 2 and parts[1].lower() == 'me':
            chan = channel if public else (parts[2] if len(parts) >= 3 else None)
            if chan:
                c.mode(chan, f'+o {irc.client.NickMask(mask).nick}')
            return

        if cmd == 'join' and len(parts) >= 2:
            chan = parts[1] if parts[1].startswith('#') else f'#{parts[1]}'
            c.join(chan)
            db.chan_add(chan)
            return

        if cmd == 'leave':
            chan = channel if public else (parts[1] if len(parts) >= 2 else None)
            if chan:
                c.part(chan)
                db.chan_remove(chan)
            return

        if level != 'admin':
            return

        if cmd == 'adduser' and len(parts) >= 3:
            target_nick = parts[1]
            new_level   = parts[2].lower()
            if new_level not in ('peon', 'admin'):
                c.privmsg(reply, 'Level must be peon or admin.')
                return
            self._whois_then(target_nick, self._adduser_cb, reply, new_level)
            return

        if cmd == 'nick' and len(parts) >= 2:
            new_nick = parts[1]
            c.nick(new_nick)
            db.cfg_set('nick', new_nick)
            return

        if cmd == 'query' and len(parts) >= 2:
            sql = text[len('query'):].strip().strip('"\'')
            try:
                cols, rows = db.run_query(sql)
                if not rows:
                    c.privmsg(reply, '(no results)')
                    return
                c.privmsg(reply, ' | '.join(cols))
                for row in rows[:10]:
                    c.privmsg(reply, ' | '.join(str(v) for v in row))
                if len(rows) > 10:
                    c.privmsg(reply, f'… {len(rows)-10} more rows omitted')
            except Exception as exc:
                c.privmsg(reply, f'SQL error: {exc}')
            return

        if cmd == 'server' and len(parts) >= 2:
            host = parts[1]
            port = int(parts[2]) if len(parts) >= 3 else 6667
            c.privmsg(reply, f'Switching to {host}:{port}…')
            self.reconnect_requested = (host, port)
            return

        if cmd == 'addserver' and len(parts) >= 2:
            host = parts[1]
            port = int(parts[2]) if len(parts) >= 3 else 6667
            db.srv_add(host, port)
            c.privmsg(reply, f'Added {host}:{port}')
            return

        if cmd == 'delserver' and len(parts) >= 2:
            db.srv_delete(parts[1])
            c.privmsg(reply, f'Removed {parts[1]}')
            return

    def _whois_then(self, nick, cb, *args):
        self.whois_pending[nick.lower()] = {'cb': cb, 'args': args}
        if self._conn and self._conn.is_connected():
            self._conn.whois(nick)

    def _adduser_cb(self, hostmask, reply_target, level):
        db.user_add(hostmask, level)
        self._safe_privmsg(reply_target, f'Added {hostmask} as {level}.')

    # ── Mode list / query numeric replies ─────────────────────────────────────

    def _on_banlist(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        mask    = e.arguments[1] if len(e.arguments) > 1 else ''
        setter  = e.arguments[2] if len(e.arguments) > 2 else ''
        info    = mask + (f'  (set by {setter})' if setter else '')
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': f'Ban: {info}', 'timestamp': _now()})

    def _on_endofbanlist(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': 'End of ban list.', 'timestamp': _now()})

    def _on_channelmodeis(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        modes   = ' '.join(e.arguments[1:]) if len(e.arguments) > 1 else ''
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': f'Mode {channel}: {modes}', 'timestamp': _now()})

    def _on_exceptlist(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        mask    = e.arguments[1] if len(e.arguments) > 1 else ''
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': f'Exception: {mask}', 'timestamp': _now()})

    def _on_endofexceptlist(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': 'End of exception list.', 'timestamp': _now()})

    def _on_liststart(self, c, e):
        self.list_acc = []
        state.buffer_push('*list*', {'type': 'list_start', 'channel': '*list*', 'timestamp': _now()})

    def _on_list(self, c, e):
        # args: [channel, num_users, topic]
        if len(e.arguments) < 2:
            return
        channel = e.arguments[0]
        users   = e.arguments[1]
        topic   = e.arguments[2] if len(e.arguments) > 2 else ''
        self.list_acc.append({
            'channel': channel,
            'users': int(users) if str(users).isdigit() else 0,
            'topic': topic,
        })

    def _on_listend(self, c, e):
        state.buffer_push('*list*', {'type': 'list_result', 'channel': '*list*',
                                      'channels': self.list_acc, 'timestamp': _now()})
        self.list_acc = []

    def _on_invitelist(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        mask    = e.arguments[1] if len(e.arguments) > 1 else ''
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': f'Invite: {mask}', 'timestamp': _now()})

    def _on_endofinvitelist(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        if channel:
            _push(channel, {'type': 'server', 'nick': '', 'text': 'End of invite list.', 'timestamp': _now()})

    # ── Error numerics ────────────────────────────────────────────────────────

    def _on_chanoprivsneeded(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        msg     = e.arguments[1] if len(e.arguments) > 1 else "You're not channel operator"
        if channel:
            _push(channel, {'type': 'error', 'nick': '', 'text': msg, 'timestamp': _now()})

    def _on_cannotsendtochan(self, c, e):
        channel = (e.arguments[0] if e.arguments else '').lower()
        reason  = e.arguments[1] if len(e.arguments) > 1 else 'Cannot send to channel'
        if not channel:
            return
        _push(channel, {
            'type':      'error',
            'nick':      '',
            'text':      f'Channel is moderated (+m) — message not sent: {reason}',
            'channel':   channel,
            'timestamp': _now(),
        })

    # ── Catch-all for server numeric replies ─────────────────────────────────

    def _on_raw_numeric(self, c, e):
        raw = e.arguments[0] if e.arguments else ''
        # :server NUMERIC botnick [#channel] [rest...]
        tokens = raw.split(' ', 4)
        if len(tokens) < 2:
            return
        numeric = tokens[1]
        if not numeric.isdigit() or numeric in self._handled_numerics:
            return
        # tokens[3] is the first payload token (after server + numeric + botnick)
        payload = tokens[3:]
        channel = None
        if payload and payload[0].startswith('#'):
            channel = payload[0].lower()
            rest = payload[1].lstrip(':') if len(payload) > 1 else ''
        else:
            rest = ' '.join(p.lstrip(':') for p in payload)
        text   = f'[{numeric}] {rest}' if rest else f'[{numeric}]'
        target = channel or '*status*'
        _push(target, {'type': 'server', 'nick': '', 'text': text, 'timestamp': _now()})

    # ── Web-initiated actions ─────────────────────────────────────────────────

    def web_whois(self, nick, reply_channel):
        self.whois_web[nick.lower()] = reply_channel
        if self._conn and self._conn.is_connected():
            self._conn.whois(nick)

    def web_kick(self, channel, nick, reason=''):
        if self._conn and self._conn.is_connected():
            self._conn.kick(channel, nick, reason)

    def web_mode(self, channel, mode, arg=''):
        if self._conn and self._conn.is_connected():
            self._conn.mode(channel, f'{mode} {arg}'.strip())

    def web_ban(self, channel, nick):
        hm = self.hostmask_cache.get(nick.lower(), '')
        if hm:
            _, _, host = hm.partition('@')
            mask = f'*!*@{host}'
        else:
            mask = f'{nick}!*@*'
        self.web_mode(channel, '+b', mask)

    def web_topic(self, channel, topic):
        if self._conn and self._conn.is_connected():
            self._conn.topic(channel, topic)

    def web_raw(self, raw):
        if self._conn and self._conn.is_connected():
            self._conn.send_raw(raw)

    def web_whois_idle(self, nick, reply_channel):
        """WHOIS with the nick repeated as both target and mask (irssi's /WII trick) —
        this forces a remote-routed lookup that includes idle/signon time even for
        servers that omit it from a plain single-argument WHOIS."""
        self.whois_web[nick.lower()] = reply_channel
        if self._conn and self._conn.is_connected():
            self._conn.send_raw(f'WHOIS {nick} {nick}')

    def web_disconnect(self, msg='Disconnected via web'):
        self._manual_disconnect = True
        if self._conn and self._conn.is_connected():
            self._conn.quit(msg)
        else:
            _push('*status*', {'type': 'disconnect', 'nick': '', 'text': 'Already disconnected.', 'timestamp': _now()})

    def web_reconnect(self):
        self._manual_disconnect = False
        if self._conn and self._conn.is_connected():
            host, port = db.srv_next()
            self.reconnect_requested = (host, port)
        else:
            self.connect()

    def web_privmsg(self, target, text):
        """Send a PRIVMSG from the web UI.

        IRC servers do not echo your own PRIVMSGs back to you, so unlike every
        other event in this file, nothing will call `_push` for this unless we
        do it here — that's the "self-sent messages aren't logged" bug.
        """
        if not (self._conn and self._conn.is_connected()):
            return False
        self._conn.privmsg(target, text)
        hm = self.hostmask_cache.get(self.nick.lower(), '')
        _push(target.lower(), {'type': 'privmsg', 'nick': self.nick, 'hostmask': hm,
                                'text': text, 'channel': target.lower(), 'timestamp': _now()})
        return True

    def web_action(self, target, text):
        """Send a CTCP ACTION (/me) from the web UI and log it, same reasoning as web_privmsg."""
        if not (self._conn and self._conn.is_connected()):
            return False
        self._conn.action(target, text)
        hm = self.hostmask_cache.get(self.nick.lower(), '')
        _push(target.lower(), {'type': 'action', 'nick': self.nick, 'hostmask': hm,
                                'text': text, 'channel': target.lower(), 'timestamp': _now()})
        return True
