"""Single background worker that serializes every Ollama query.

Each query holds an SSH tunnel open for its duration (seconds, easily longer
for a slow model), so nothing that runs on the IRC reactor thread may call
ollama_tunnel.generate() directly — that would freeze the whole bot (every
channel, PING/PONG, everything) for as long as the query takes.

Instead, both the manual `ollama` command (plugins/ollama.py) and the
hostmask-watch auto-trigger (ai_watch.py) call submit() here. Jobs run one at
a time on a single worker thread in the order they arrived — if a burst of
triggers fires while a query is already in flight, they simply wait their
turn instead of racing for the tunnel or getting dropped.

The channel only ever hears about successful Ollama responses. A failed job
(tunnel down, auth failure, Ollama HTTP error, ...) is logged and dropped
silently rather than posted — a successful reply is handed to
state.queue_send, which the bot's main loop drains and sends over IRC
without the worker needing any reference to the live connection.
"""

import logging
import queue
import threading

import db
import ollama_tunnel
import state

log = logging.getLogger('stegobot')

_MAX_LINE_B = 400
_MAX_LINES  = 3

_queue   = queue.Queue()
_started = False  # only ever touched from the reactor thread via submit()


def submit(target, nick, lines, system_prompt=None):
    """Enqueue an Ollama job for `nick`'s `lines`; reply/replies are delivered
    asynchronously to `target` (a channel or nick) via state.queue_send once
    the query returns — possibly after waiting behind other queued jobs.

    `system_prompt` is per-job (each watched user can have their own — see
    ai_watch.py) rather than a single global setting; it falls back to
    ollama_tunnel.DEFAULT_SYSTEM_PROMPT when not given."""
    global _started
    if not _started:
        threading.Thread(target=_worker_loop, daemon=True, name='ollama-worker').start()
        _started = True
    _queue.put((target, nick, lines, system_prompt))


def _worker_loop():
    while True:
        target, nick, lines, system_prompt = _queue.get()
        try:
            _run_job(target, nick, lines, system_prompt)
        except Exception:
            log.exception('ollama_worker: job failed (target=%s nick=%s)', target, nick)
        finally:
            _queue.task_done()


def _run_job(target, nick, lines, system_prompt):
    system_prompt = system_prompt or ollama_tunnel.DEFAULT_SYSTEM_PROMPT
    model         = db.cfg_get('ollama_model', ollama_tunnel.DEFAULT_MODEL)
    prompt = (
        f"Last {len(lines)} thing(s) said by {nick}:\n" +
        '\n'.join(f'- {l}' for l in lines)
    )
    log.info('ollama_worker: querying for %s (%d bytes, queue depth now %d)',
             nick, len(prompt.encode()), _queue.qsize())
    try:
        text = ollama_tunnel.generate(prompt, model=model, system=system_prompt)
    except Exception as exc:
        # Errors (tunnel down, auth failure, Ollama HTTP error, etc.) are logged
        # only — the channel should only ever see a successful Ollama response,
        # never plumbing failures.
        log.warning('ollama_worker: Ollama error for %s: %s', nick, exc)
        return

    lines_out = [l.strip() for l in text.replace('\r', '').split('\n') if l.strip()][:_MAX_LINES]
    for line in lines_out:
        if len(line) > _MAX_LINE_B:
            line = line[:_MAX_LINE_B - 1] + '…'
        state.queue_send(target, line)
