"""Urban Dictionary lookup plugin.

Usage (in channel):  <botnick>: urbandictionary <word>
Usage (in PM):       urbandictionary <word>

Anyone can use this (PUBLIC = True).
"""

import re
import time
import requests

COMMANDS = ['urbandictionary', 'ud']
PUBLIC   = True   # no auth required

_API = 'https://api.urbandictionary.com/v0/define'
_BRACKET_RE = re.compile(r'\[|\]')  # UD wraps related words in [brackets]


def handle(cmd, args, ctx):
    word = args.strip()
    if not word:
        ctx['reply']('Usage: urbandictionary <word>  (alias: ud)')
        return True

    try:
        r = requests.get(_API, params={'term': word}, timeout=8,
                         headers={'User-Agent': 'stegobot/1.0'})
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as exc:
        ctx['reply'](f'UD API error: {exc}')
        return True

    entries = data.get('list', [])
    if not entries:
        ctx['reply'](f'No definition found for "{word}".')
        return True

    top = entries[0]
    defn  = _BRACKET_RE.sub('', top.get('definition', '')).replace('\r\n', ' ').replace('\n', ' ').strip()
    example = _BRACKET_RE.sub('', top.get('example', '')).replace('\r\n', ' ').replace('\n', ' ').strip()
    thumbs  = f"↑{top.get('thumbs_up', 0)} ↓{top.get('thumbs_down', 0)}"

    # Truncate to fit IRC line limits (450 chars)
    if len(defn) > 400:
        defn = defn[:397] + '...'

    ctx['reply'](f'\x02{word}\x02: {defn}  [{thumbs}]')
    if example:
        if len(example) > 300:
            example = example[:297] + '...'
        time.sleep(0.5)
        ctx['reply'](f'Example: {example}')

    return True
