"""Shared mutable state between the IRC bot thread and the Flask web thread."""

import threading
from collections import defaultdict, deque

bot_instance = None          # set by stegobot.py after bot is created
socketio_instance = None     # set by web/app.py after SocketIO is created

# IRC→web: per-channel ring buffers (last 500 lines per channel)
channel_buffers = defaultdict(lambda: deque(maxlen=500))
buffer_lock = threading.Lock()

# web→IRC: commands/messages to be sent on the next bot tick
send_queue = []
send_lock = threading.Lock()


def buffer_push(channel, entry):
    with buffer_lock:
        channel_buffers[channel].append(entry)
    if socketio_instance:
        try:
            socketio_instance.emit('irc_event', entry, namespace='/')
        except Exception:
            pass


def queue_send(target, text):
    with send_lock:
        send_queue.append((target, text))


def drain_send_queue():
    with send_lock:
        items = list(send_queue)
        send_queue.clear()
    return items
