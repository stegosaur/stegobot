"""Hot-reloading plugin loader for StegoBot.

Each plugin in plugins/*.py may define:
  COMMANDS = ['word1', 'word2']          # command names this plugin handles
  PUBLIC   = True                        # if True, called even for unauthed users
  def handle(cmd, args, ctx) -> bool:   # return True if handled, None/False to skip

ctx dict passed to handle():
  nick     – sender's nick
  channel  – channel name (if public message) or None
  public   – True if in a channel, False if PM
  level    – 'peon', 'admin', or None (unauthenticated)
  reply    – callable(str): sends a reply to the right place
  conn     – irc ServerConnection
"""

import importlib.util
import logging
from pathlib import Path

log = logging.getLogger('stegobot')

PLUGIN_DIR = Path('/opt/stegobot/plugins')

# cmd_lower -> module
_registry: dict = {}
# path_str -> mtime
_mtimes: dict = {}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reload_changed():
    for path in sorted(PLUGIN_DIR.glob('*.py')):
        if path.name.startswith('_'):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path)
        if _mtimes.get(key) == mtime:
            continue
        try:
            mod = _load(path)
            _mtimes[key] = mtime
            for cmd in getattr(mod, 'COMMANDS', []):
                _registry[cmd.lower()] = mod
            log.info('Plugin loaded: %s (%s)', path.name,
                     ', '.join(getattr(mod, 'COMMANDS', [])))
        except Exception:
            log.exception('Failed to load plugin %s', path.name)


def dispatch(cmd: str, args: str, ctx: dict):
    """Try to handle cmd with a plugin.

    Returns True if a plugin handled it, None otherwise.
    Call this BEFORE the built-in level gate so PUBLIC plugins work for everyone.
    """
    _reload_changed()
    mod = _registry.get(cmd.lower())
    if mod is None:
        return None
    # Respect PUBLIC flag: if not PUBLIC and user is unauthenticated, skip
    if not getattr(mod, 'PUBLIC', False) and ctx.get('level') is None:
        return None
    if not hasattr(mod, 'handle'):
        return None
    try:
        result = mod.handle(cmd.lower(), args, ctx)
        return True if result else None
    except Exception:
        log.exception('Plugin %s raised an exception', cmd)
        return None
