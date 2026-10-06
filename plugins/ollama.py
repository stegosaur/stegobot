"""Ollama AI plugin — queries a remote Ollama server over an SSH tunnel.

Usage (channel):  <botnick>: ollama [nick]
Usage (PM):       ollama [nick]     (PM has no channel, so nothing to say)

Takes the last N things <nick> said in the current channel (N =
ollama_tunnel.DEFAULT_LINES) and asks Ollama to respond, using
ollama_tunnel.DEFAULT_SYSTEM_PROMPT for context. Defaults to the calling
user's own nick if none is given. Unlike the AI Watchlist's per-watched-user
line count/prompt (see ai_watch.py — those are configurable per hostmask
since different people warrant different framing), this on-demand command
can target anyone, so it just uses the module's fixed defaults rather than a
single global "everyone gets this" setting. The query itself runs on
ollama_worker's background thread (see that module) rather than blocking
here, since it can take a while and this command is on the same shared IRC
reactor thread that every other channel/connection needs — the reply arrives
whenever it's ready, queued behind any other Ollama job already in flight.

SSH host/user/port, remote/local port, model and query timeout are
overridable from the web config page (keys: ollama_ssh_host, ollama_ssh_user,
ollama_ssh_port, ollama_remote_port, ollama_local_port, ollama_model,
ollama_query_timeout). This command is not gated by the AI Watchlist — that
only controls automatic replies; anyone can ask about anyone with this
command.

PUBLIC = True so anyone can use it.
"""

import gzip
import re
from pathlib import Path

import ollama_tunnel
import ollama_worker

COMMANDS = ['ollama']
PUBLIC   = True

LOG_DIR = Path('/opt/stegobot/logs')

# Matches ChannelLog.write's format: [ISO-timestamp] type nick text
_LOG_LINE_RE = re.compile(r'^\[[^\]]+\] (\S+) (\S*) (.*)$')
_SPOKEN_TYPES = ('privmsg', 'action')


def _tail_lines_for_nick(channel, nick, limit):
    """Return up to `limit` most recent lines <nick> said in `channel`, oldest first."""
    safe   = channel.lstrip('#').replace('/', '_').lower()
    nick_l = nick.lower()
    collected = []

    def scan(lines):
        for line in reversed(lines):
            m = _LOG_LINE_RE.match(line.strip())
            if not m:
                continue
            kind, line_nick, text = m.groups()
            if kind not in _SPOKEN_TYPES or line_nick.lower() != nick_l:
                continue
            collected.append(text)
            if len(collected) >= limit:
                return True
        return False

    log_files = sorted(LOG_DIR.glob(f'{safe}_*.log'))
    if log_files:
        try:
            with open(log_files[-1], 'r', encoding='utf-8', errors='replace') as f:
                if scan(f.readlines()):
                    collected.reverse()
                    return collected
        except Exception:
            pass

    gz_files = sorted(LOG_DIR.glob(f'{safe}_*.log.gz'), reverse=True)
    for gz in gz_files:
        try:
            with gzip.open(gz, 'rt', encoding='utf-8', errors='replace') as f:
                if scan(f.readlines()):
                    break
        except Exception:
            continue

    collected.reverse()
    return collected


def handle(cmd, args, ctx):
    channel = ctx.get('channel')
    if not channel:
        ctx['reply']('ollama only works inside a channel.')
        return True

    target = args.strip() or ctx.get('nick')
    lines = _tail_lines_for_nick(channel, target, ollama_tunnel.DEFAULT_LINES)
    if not lines:
        ctx['reply'](f'No recent lines found for {target} in {channel}.')
        return True

    ollama_worker.submit(channel, target, lines)
    return True
