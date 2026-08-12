"""Gemini AI plugin — powered by google-genai SDK (gemini-flash-latest).

Commands:
  prompt <question>          — ask Gemini anything
  ai <question>              — alias for prompt
  channelprompt <question>   — ask Gemini about this channel's recent logs

PUBLIC = True so anyone can use it.
API key is read from /opt/stegobot/.env (GEMINI_API_KEY).
Logs fed to channelprompt are capped at 2 MB uncompressed.
"""

import gzip
import logging
import re
import os
import time
from pathlib import Path

log = logging.getLogger('stegobot')

LOG_DIR        = Path('/opt/stegobot/logs')
_MAX_LOG_BYTES = 2 * 1024 * 1024   # 2 MB hard cap per channelprompt
_MAX_LINE_B    = 400                # max bytes for message text (512 limit includes PRIVMSG overhead)
_MAX_LINES     = 5                  # max IRC lines per response

# Instruction prepended to every request so Gemini formats for IRC
_IRC_SYSTEM = (
    "You are a concise IRC bot assistant. Rules for ALL responses:\n"
    "1. maximum 400 lines per msgs. maximum 2 messages. keep responses short enough for irc setting."
    "2. avoid using newlines. if you want to give bullet points that's fine, just keep them in line"
    "and seperate each one with ' (•) '"
    "3. Be direct and information-dense; fill each line to capacity.\n"
)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    env_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get('GEMINI_API_KEY', '')
    if not key:
        raise RuntimeError('GEMINI_API_KEY not set in .env')
    from google import genai
    _client = genai.Client(api_key=key)
    return _client


COMMANDS = ['prompt', 'ai', 'channelprompt']
PUBLIC   = True

_MODEL = 'gemini-flash-latest'

# Bullet/list markers to collapse inline
_BULLET_RE = re.compile(r'\n\s*(?:[•\-\*]|\d+[\.\)])\s*')
# Leftover markdown symbols
_MD_RE = re.compile(r'[*_`#]+')


def _ask(prompt_text):
    client = _get_client()
    full = f"{_IRC_SYSTEM}\n\n{prompt_text}"
    log.info('Gemini prompt (%d bytes):\n%s', len(full.encode()), full)
    try:
        response = client.models.generate_content(model=_MODEL, contents=full)
    except Exception as exc:
        code = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
        if code == 429 or '429' in str(exc)[:20] or 'RESOURCE_EXHAUSTED' in str(exc):
            raise RuntimeError('Gemini is out of free-tier quota. Try again later.') from exc
        raise
    return response.text.strip()


def _clean(text):
    """Collapse bullet lists inline and strip markdown."""
    # Collapse bullet/numbered list items into inline ' (•) ' separated string
    text = _BULLET_RE.sub(' (•) ', text)
    # Strip leftover markdown
    text = _MD_RE.sub('', text)
    return text.strip()


def _split_response(text):
    """Split into IRC lines: max _MAX_LINES, each ≤ _MAX_LINE_B bytes."""
    text = _clean(text)
    raw_lines = [l.strip() for l in text.split('\n') if l.strip()]

    # First pass: hard-split any line that exceeds the byte limit
    chunks = []
    for raw in raw_lines:
        encoded = raw.encode('utf-8')
        while encoded:
            if len(encoded) <= _MAX_LINE_B:
                chunks.append(encoded.decode('utf-8', errors='replace').strip())
                break
            chunk = encoded[:_MAX_LINE_B]
            cut = chunk.rfind(b' ')
            if cut > 0:
                chunk = encoded[:cut]
            chunks.append(chunk.decode('utf-8', errors='replace').strip())
            encoded = encoded[len(chunk):].lstrip(b' ')

    # Second pass: greedily merge adjacent chunks if they fit on one line
    merged = []
    i = 0
    while i < len(chunks):
        current = chunks[i]
        while i + 1 < len(chunks):
            combined = current + ' ' + chunks[i + 1]
            if len(combined.encode('utf-8')) <= _MAX_LINE_B:
                current = combined
                i += 1
            else:
                break
        merged.append(current)
        i += 1

    out = merged[:_MAX_LINES]

    # Mark truncation if content was dropped
    full_bytes = len(text.encode('utf-8'))
    sent_bytes = sum(len(l.encode('utf-8')) for l in out)
    if sent_bytes < full_bytes - 20 and out:
        out[-1] = out[-1].rstrip('.') + '…'

    return out


def _irc_target(ctx):
    """Return the PRIVMSG target (channel or nick) for length checks."""
    return ctx.get('channel') or ctx.get('nick') or 'x'


def _fits_irc(line, target):
    """True if PRIVMSG target :line\r\n fits within 512 bytes."""
    return len(f'PRIVMSG {target} :{line}\r\n'.encode('utf-8')) <= 512


def _needs_tldr(lines, target):
    return any(not _fits_irc(l, target) for l in lines)


def _tldr(original_text):
    """Ask Gemini to condense a response that was too long."""
    prompt = (
        'Condense the following into at most 3 plain-text lines, '
        'each under 350 characters, no markdown, '
        "bullet points inline with ' (•) ':\n\n" + original_text
    )
    return _ask(prompt)


def _send(ctx, lines):
    for i, line in enumerate(lines):
        if i > 0:
            time.sleep(0.5)
        try:
            ctx['reply'](line)
        except Exception as exc:
            try:
                ctx['reply'](f'[send error: {exc}]')
            except Exception:
                pass
            break


def _read_channel_log(channel):
    """Return up to _MAX_LOG_BYTES of recent log text for channel."""
    safe = channel.lstrip('#').replace('/', '_').lower()
    parts = []
    total = 0

    log_files = sorted(LOG_DIR.glob(f'{safe}_*.log'))
    if log_files:
        try:
            with open(log_files[-1], 'r', encoding='utf-8', errors='replace') as f:
                data = f.read(_MAX_LOG_BYTES)
                parts.append(data)
                total += len(data.encode('utf-8'))
        except Exception:
            pass

    if total < _MAX_LOG_BYTES:
        gz_files = sorted(LOG_DIR.glob(f'{safe}_*.log.gz'))
        if gz_files:
            try:
                remaining = _MAX_LOG_BYTES - total
                with gzip.open(gz_files[-1], 'rt', encoding='utf-8', errors='replace') as f:
                    data = f.read(remaining)
                    parts.insert(0, data)
            except Exception:
                pass

    return '\n'.join(parts)


def handle(cmd, args, ctx):
    query = args.strip()

    if cmd == 'channelprompt':
        if not query:
            ctx['reply']('Usage: channelprompt <question about this channel>')
            return True
        channel = ctx.get('channel') or ''
        if not channel:
            ctx['reply']('channelprompt only works inside a channel.')
            return True
        ctx['reply'](f'Fetching logs for {channel}…')
        log_text = _read_channel_log(channel)
        if not log_text.strip():
            ctx['reply'](f'No log data found for {channel}.')
            return True
        size_kb = len(log_text.encode('utf-8')) / 1024
        full_prompt = (
            f"These are IRC chat logs for {channel} ({size_kb:.0f} KB). "
            f"Answer the following based only on these logs:\n\n"
            f"{query}\n\n"
            f"--- LOGS ---\n{log_text}\n--- END LOGS ---"
        )
        try:
            text = _ask(full_prompt)
        except Exception as exc:
            ctx['reply'](f'Gemini error: {exc}')
            return True
        _send_safe(ctx, text)
        return True

    # prompt / ai
    if not query:
        ctx['reply']('Usage: prompt <question>')
        return True
    try:
        text = _ask(query)
    except Exception as exc:
        ctx['reply'](f'Gemini error: {exc}')
        return True
    _send_safe(ctx, text)
    return True


def _send_safe(ctx, text):
    """Split, TL;DR-retry if any line is too long, then send."""
    target = _irc_target(ctx)
    lines  = _split_response(text)
    if _needs_tldr(lines, target):
        log.info('Response too long for IRC, requesting TL;DR')
        try:
            text = _tldr(text)
        except Exception as exc:
            ctx['reply'](f'Gemini error (tldr): {exc}')
            return
        lines = _split_response(text)
    _send(ctx, lines)
