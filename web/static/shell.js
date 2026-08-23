// Browser shell — a real bash pty on the host, running as the bot's own
// user account. See web/app.py's /shell socket.io namespace for the backend.
(function () {
  const term = new Terminal({
    fontFamily: "'Cascadia Code','Fira Mono','Consolas',monospace",
    fontSize: 14,
    cursorBlink: true,
    theme: { background: '#0d0d0d', foreground: '#d4d4d4' },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById('shell-container'));
  fit.fit();
  term.focus();

  const socket = io('/shell', { transports: ['websocket', 'polling'] });

  function sendResize() {
    fit.fit();
    socket.emit('resize', { cols: term.cols, rows: term.rows });
  }

  socket.on('connect', () => {
    term.reset();
    sendResize();
  });
  socket.on('output', (data) => term.write(data));
  socket.on('exit', () => term.write('\r\n\x1b[31m[shell exited]\x1b[0m\r\n'));
  socket.on('disconnect', () => term.write('\r\n\x1b[31m[disconnected]\x1b[0m\r\n'));

  term.onData((data) => socket.emit('input', data));

  if (window.ResizeObserver) {
    new ResizeObserver(sendResize).observe(document.getElementById('shell-container'));
  } else {
    window.addEventListener('resize', sendResize);
  }
})();
