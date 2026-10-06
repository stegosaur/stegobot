"""SSH tunnel to a remote Ollama server, used by plugins/ollama.py.

The bot host reaches Ollama by opening a local SSH port-forward (using the
`stegobot` system user's existing SSH trust) rather than exposing Ollama's
HTTP API directly. The tunnel is opened fresh for each query and torn down
right after — nothing is left running between requests.

All settings below have config-table overrides (see db.cfg_get) so they can
be changed from the web UI without a restart; the constants here are just
the defaults shown/used when a key hasn't been set.
"""

import http.client
import json
import logging
import os
import socket
import subprocess
import threading
import time

import db

log = logging.getLogger('stegobot')

DEFAULT_SSH_HOST     = 'stegosaur.org'
DEFAULT_SSH_USER     = 'stegosaur'
DEFAULT_SSH_PORT     = 222
DEFAULT_REMOTE_PORT  = 11434
DEFAULT_LOCAL_PORT   = 11435
DEFAULT_MODEL        = 'llama3.2'
DEFAULT_LINES        = 20
DEFAULT_QUERY_TIMEOUT = 600  # seconds to wait for Ollama's HTTP response
DEFAULT_SYSTEM_PROMPT = (
    "You are an IRC bot. You will be given the last few things a specific chat "
    "user said. Reply about them in 1-3 short IRC-friendly lines: no markdown, "
    "no newlines within a line."
)

_TUNNEL_LOCK = threading.Lock()  # serializes tunnel open/query/close


def _cfg():
    return {
        'host':        db.cfg_get('ollama_ssh_host', DEFAULT_SSH_HOST),
        'user':        db.cfg_get('ollama_ssh_user', DEFAULT_SSH_USER),
        'ssh_port':    int(db.cfg_get('ollama_ssh_port', DEFAULT_SSH_PORT) or DEFAULT_SSH_PORT),
        'remote_port': int(db.cfg_get('ollama_remote_port', DEFAULT_REMOTE_PORT) or DEFAULT_REMOTE_PORT),
        'local_port':  int(db.cfg_get('ollama_local_port', DEFAULT_LOCAL_PORT) or DEFAULT_LOCAL_PORT),
        'model':       db.cfg_get('ollama_model', DEFAULT_MODEL),
        'timeout':     int(db.cfg_get('ollama_query_timeout', DEFAULT_QUERY_TIMEOUT) or DEFAULT_QUERY_TIMEOUT),
    }


def _wait_for_port(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _open_tunnel(cfg):
    identity = os.path.expanduser('~/.ssh/id_ed25519')
    cmd = [
        'ssh', '-N',
        '-p', str(cfg['ssh_port']),
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=10',
        '-o', 'IdentitiesOnly=yes',
        '-i', identity,
        '-L', f"127.0.0.1:{cfg['local_port']}:localhost:{cfg['remote_port']}",
        f"{cfg['user']}@{cfg['host']}",
    ]
    log.info('Opening SSH tunnel to %s@%s:%d (local :%d -> remote :%d)',
             cfg['user'], cfg['host'], cfg['ssh_port'], cfg['local_port'], cfg['remote_port'])
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not _wait_for_port(cfg['local_port']):
        err = ''
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=3)
            err = stderr.decode(errors='replace').strip()
        except Exception:
            proc.kill()
        raise RuntimeError(
            f"SSH tunnel to {cfg['host']}:{cfg['ssh_port']} didn't come up" + (f': {err}' if err else '')
        )
    return proc


def _close_tunnel(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def query_ollama(local_port, model, prompt, system=None, timeout=DEFAULT_QUERY_TIMEOUT):
    """POST to Ollama's /api/generate over an already-open local port; return the reply text.

    `system` maps to Ollama's own `system` field on this endpoint — the preceding
    context prompt sent ahead of `prompt` so the model knows how it's meant to reply.
    """
    # think=False: reasoning-capable models (e.g. Qwen3) otherwise prepend their
    # chain-of-thought as plain text ahead of the actual reply, which is all
    # this bot would end up posting to IRC as the "response" — it has no
    # separate handling for a `thinking` field. This also cuts query time
    # dramatically (the model skips generating the reasoning tokens at all).
    payload = {'model': model, 'prompt': prompt, 'stream': False, 'think': False}
    if system:
        payload['system'] = system
    body = json.dumps(payload)
    conn = http.client.HTTPConnection('127.0.0.1', local_port, timeout=timeout)
    try:
        conn.request('POST', '/api/generate', body=body,
                     headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f'Ollama HTTP {resp.status}: {data.decode(errors="replace")[:200]}')
    finally:
        conn.close()
    result = json.loads(data)
    return (result.get('response') or '').strip()


def generate(prompt, model=None, system=None):
    """Open the tunnel, query Ollama, close the tunnel again, and return the reply text."""
    with _TUNNEL_LOCK:
        cfg = _cfg()
        model = model or cfg['model']
        proc = _open_tunnel(cfg)
        try:
            return query_ollama(cfg['local_port'], model, prompt, system=system, timeout=cfg['timeout'])
        finally:
            _close_tunnel(proc)
