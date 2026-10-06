#!/usr/bin/env python3
"""Entry point — starts the IRC bot and web server in parallel threads."""

import logging
import signal
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

import db
import state
from bot import StegoBot

def main():
    db.init_schema()

    bot = StegoBot()
    state.bot_instance = bot

    # Without this, systemd's plain SIGTERM (every `systemctl restart`/`stop`)
    # kills the process with no IRC QUIT ever sent — the server has no idea
    # we're gone until its own ping-timeout fires, sometimes minutes later,
    # and until then our nick is a "ghost" that a fresh connect() collides
    # with. A network-wide-nick network (this bot runs on EFnet) makes that
    # collision visible on every server, not just the one we were on.
    def _handle_term(signum, frame):
        bot.should_stop = True
        bot.disconnect('Restarting')

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    # Start web server in a daemon thread
    from web.app import create_app, socketio
    app = create_app()
    state.socketio_instance = socketio

    web_port = int(db.cfg_get('web_port', '8080'))

    web_thread = threading.Thread(
        target=lambda: socketio.run(app, host='127.0.0.1', port=web_port,
                                    use_reloader=False, log_output=False,
                                    allow_unsafe_werkzeug=True),
        daemon=True,
        name='web'
    )
    web_thread.start()
    logging.getLogger('stegobot').info('Web server started on 127.0.0.1:%s', web_port)

    # Connect and run the IRC bot (blocks)
    bot.connect()
    bot.run()


if __name__ == '__main__':
    main()
