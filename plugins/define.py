"""Dictionary definition plugin — powered by dictionaryapi.dev.

Usage (channel):  <botnick>: define <word>
Usage (PM):       define <word>

PUBLIC = True so anyone can use it.
"""

import time
import requests

COMMANDS = ['define', 'def']
PUBLIC   = True

_API = 'https://api.dictionaryapi.dev/api/v2/entries/en/{}'


def handle(cmd, args, ctx):
    word = args.strip()
    if not word:
        ctx['reply']('Usage: define <word>  (alias: def)')
        return True

    try:
        r = requests.get(_API.format(requests.utils.quote(word)), timeout=8,
                         headers={'User-Agent': 'stegobot/1.0'})
    except requests.RequestException as exc:
        ctx['reply'](f'Dictionary API error: {exc}')
        return True

    if r.status_code == 404:
        ctx['reply'](f'No definition found for "{word}".')
        return True

    if not r.ok:
        ctx['reply'](f'Dictionary API error: HTTP {r.status_code}')
        return True

    data = r.json()
    if not data or not isinstance(data, list):
        ctx['reply'](f'No definition found for "{word}".')
        return True

    entry = data[0]
    display_word = entry.get('word', word)
    phonetic     = entry.get('phonetic', '')

    # Collect up to 3 definitions across meanings
    defs = []
    for meaning in entry.get('meanings', []):
        pos = meaning.get('partOfSpeech', '')
        for defn in meaning.get('definitions', []):
            text    = defn.get('definition', '').strip()
            example = defn.get('example', '').strip()
            if text:
                defs.append((pos, text, example))
            if len(defs) >= 3:
                break
        if len(defs) >= 3:
            break

    if not defs:
        ctx['reply'](f'No definition found for "{word}".')
        return True

    B = '\x02'
    header = f'{B}{display_word}{B}'
    if phonetic:
        header += f' {phonetic}'

    for i, (pos, text, example) in enumerate(defs):
        if len(text) > 400:
            text = text[:397] + '...'
        num = f'[{i+1}/{len(defs)}] ' if len(defs) > 1 else ''
        pos_str = f'({pos}) ' if pos else ''
        if i > 0:
            time.sleep(0.5)
        if i == 0:
            ctx['reply'](f'{header} — {num}{pos_str}{text}')
        else:
            ctx['reply'](f'{num}{pos_str}{text}')
        if example:
            if len(example) > 300:
                example = example[:297] + '...'
            time.sleep(0.5)
            ctx['reply'](f'e.g. {example}')

    return True
