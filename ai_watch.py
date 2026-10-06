"""Hostmask-based auto-trigger for Ollama replies.

Only users whose hostmask matches a configured `ai_watch` entry (web config
page, "AI Watchlist") get an automatic reply, and each entry carries its own
line-count threshold and system prompt — these are per watched user, not a
global setting, since different people warrant different context/framing.
bot.py resolves the matching row and calls on_line() for every channel line
from that hostmask; once `limit` lines have accumulated, they're handed to
ollama_worker.submit() (along with that entry's system_prompt) and the count
resets. Buffering here is pure in-memory bookkeeping — the actual query runs
asynchronously on ollama_worker's single background thread, so a burst of
matching lines just queues up more jobs rather than blocking the bot.

Called only from the IRC reactor thread (bot.py's _on_pubmsg path), so the
buffer dict needs no lock.
"""

import ollama_tunnel
import ollama_worker

_buffers = {}  # (channel_lower, hostmask_lower) -> [line, line, ...]


def on_line(channel, nick, hostmask, text, limit=None, system_prompt=None):
    """`limit`/`system_prompt` come from the matched ai_watch row (may be None
    on a legacy row predating these per-entry columns, hence the fallbacks)."""
    limit = int(limit or ollama_tunnel.DEFAULT_LINES)
    key = (channel.lower(), hostmask.lower())
    buf = _buffers.setdefault(key, [])
    buf.append(text)
    if len(buf) < limit:
        return
    snapshot = buf[:limit]
    del buf[:limit]
    ollama_worker.submit(channel, nick, snapshot, system_prompt=system_prompt)
