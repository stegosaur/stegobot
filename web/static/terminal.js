'use strict';
/* StegoBot xterm.js IRC terminal */

// ── ANSI colour helpers ───────────────────────────────────────────────────────
const R = '\x1b[0m';
const B = '\x1b[1m';
const GRAY    = '\x1b[38;5;246m';   // visible light-gray for timestamps
const GREEN   = '\x1b[32m';
const BGREEN  = '\x1b[92m';
const RED     = '\x1b[31m';
const BRED    = '\x1b[91m';
const YELLOW  = '\x1b[33m';
const CYAN    = '\x1b[36m';
const BLUE    = '\x1b[34m';
const MAGENTA = '\x1b[35m';
const WHITE   = '\x1b[97m';

const NICK_COLORS = [
  '\x1b[32m','\x1b[33m','\x1b[34m','\x1b[35m','\x1b[36m',
  '\x1b[92m','\x1b[93m','\x1b[94m','\x1b[95m','\x1b[96m',
  '\x1b[31m','\x1b[91m','\x1b[37m','\x1b[97m',
];
function nickColor(nick) {
  let h = 0;
  for (const c of (nick || '')) h = ((h * 31) + c.charCodeAt(0)) & 0xffff;
  return NICK_COLORS[h % NICK_COLORS.length];
}
function nc(nick) { return `${nickColor(nick)}${nick}${R}`; }

function ts(iso) {
  const d = iso ? new Date(iso) : new Date();
  const t = d.toTimeString().slice(0, 8);
  return `${GRAY}${t}${R}`;
}

// IRC colour (mIRC 0-15) → ANSI escape: foreground and background
const _IRC_FG = [
  '\x1b[97m',  // 0  white
  '\x1b[30m',  // 1  black
  '\x1b[34m',  // 2  navy
  '\x1b[32m',  // 3  green
  '\x1b[91m',  // 4  red
  '\x1b[31m',  // 5  maroon
  '\x1b[35m',  // 6  purple
  '\x1b[33m',  // 7  orange
  '\x1b[93m',  // 8  yellow
  '\x1b[92m',  // 9  light green
  '\x1b[36m',  // 10 teal
  '\x1b[96m',  // 11 light cyan
  '\x1b[94m',  // 12 royal blue
  '\x1b[95m',  // 13 pink
  '\x1b[90m',  // 14 dark grey
  '\x1b[37m',  // 15 light grey
];
const _IRC_BG = [
  '\x1b[107m', '\x1b[40m',  '\x1b[44m',  '\x1b[42m',
  '\x1b[101m', '\x1b[41m',  '\x1b[45m',  '\x1b[43m',
  '\x1b[103m', '\x1b[102m', '\x1b[46m',  '\x1b[106m',
  '\x1b[104m', '\x1b[105m', '\x1b[100m', '\x1b[47m',
];

function ircToAnsi(s) {
  s = String(s || '').replace(/\r?\n/g, ' ');
  let out = '';
  let i = 0;
  while (i < s.length) {
    const ch = s.charCodeAt(i);
    if (ch === 0x02) { out += '\x1b[1m';  i++; continue; }  // bold
    if (ch === 0x1d) { out += '\x1b[3m';  i++; continue; }  // italic
    if (ch === 0x1f) { out += '\x1b[4m';  i++; continue; }  // underline
    if (ch === 0x16) { out += '\x1b[7m';  i++; continue; }  // reverse
    if (ch === 0x0f) { out += '\x1b[0m';  i++; continue; }  // reset all
    if (ch === 0x03) {                                        // colour
      i++;
      let fg = -1, bg = -1;
      if (i < s.length && s[i] >= '0' && s[i] <= '9') {
        fg = s.charCodeAt(i) - 48; i++;
        if (i < s.length && s[i] >= '0' && s[i] <= '9') {
          fg = fg * 10 + (s.charCodeAt(i) - 48); i++;
        }
        if (i < s.length && s[i] === ',') {
          const j = i + 1;
          if (j < s.length && s[j] >= '0' && s[j] <= '9') {
            i = j;
            bg = s.charCodeAt(i) - 48; i++;
            if (i < s.length && s[i] >= '0' && s[i] <= '9') {
              bg = bg * 10 + (s.charCodeAt(i) - 48); i++;
            }
          }
        }
      }
      if (fg === -1 && bg === -1) {
        out += '\x1b[39m\x1b[49m';   // bare \x03 resets colour
      } else {
        if (fg >= 0 && fg < 16) out += _IRC_FG[fg];
        if (bg >= 0 && bg < 16) out += _IRC_BG[bg];
      }
      continue;
    }
    out += s[i]; i++;
  }
  return out;
}

function esc(s) {
  return ircToAnsi(s) + R;
}

// ── Per-window state ──────────────────────────────────────────────────────────
// name -> { term: Terminal, fit: FitAddon, el: HTMLElement,
//           users: Map(nick -> {prefix,hostmask}), topic: '', unread: bool }
const wins = new Map();
let activeWin = '';
let socket;
let inputHistory = [];
let histIdx = -1;
let ctxTarget = { nick: '', channel: '' };

const TERM_OPTS = {
  theme: {
    background:  '#0d0d0d', foreground: '#d4d4d4', cursor: '#56b6c2',
    selectionBackground: '#264f78',
    black: '#1e1e1e', red: '#e06c75', green: '#4ec94e', yellow: '#e5c07b',
    blue: '#61afef', magenta: '#c678dd', cyan: '#56b6c2', white: '#d4d4d4',
    brightBlack: '#4e4e4e', brightRed: '#ff7070', brightGreen: '#98c379',
    brightYellow: '#e5c07b', brightBlue: '#61afef', brightMagenta: '#c678dd',
    brightCyan: '#56b6c2', brightWhite: '#ffffff',
  },
  fontFamily: "'Cascadia Code','Fira Mono','Consolas',monospace",
  fontSize: 17, lineHeight: 1.35, scrollback: 5000,
  cursorStyle: 'block', cursorBlink: false,
  convertEol: true,
};

// ── Window management ─────────────────────────────────────────────────────────

function ensureWin(name) {
  if (wins.has(name)) return wins.get(name);
  const el  = document.createElement('div');
  el.className = 'term-pane';
  document.getElementById('term-container').appendChild(el);

  const term = new Terminal(TERM_OPTS);
  const fit  = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);

  const w = { term, fit, el, users: new Map(), topic: '', unread: false };
  wins.set(name, w);
  return w;
}

function switchWin(name) {
  if (!wins.has(name) && name !== '*status*') ensureWin(name);

  // Hide current
  if (activeWin && wins.has(activeWin)) {
    wins.get(activeWin).el.classList.remove('active');
  }
  // Show new
  const w = ensureWin(name);
  w.el.classList.add('active');
  w.unread = false;
  activeWin = name;

  setTimeout(() => { w.fit.fit(); w.term.scrollToBottom(); }, 10);

  // Update UI
  document.getElementById('topic-bar').textContent = w.topic ? `${name}  —  ${w.topic}` : name;
  document.getElementById('input-prompt').textContent = `[${name}]`;
  document.querySelectorAll('.win-item').forEach(el => {
    el.classList.toggle('active', el.dataset.win === name);
    if (el.dataset.win === name) {
      el.classList.remove('unread', 'unread-mention');
    }
  });
  renderNames(w.users);
  document.getElementById('irc-input').focus();
}

function writeLine(winName, ansiText) {
  const w = ensureWin(winName);
  w.term.writeln(ansiText);
  if (winName !== activeWin) {
    w.unread = true;
    const el = document.querySelector(`.win-item[data-win="${CSS.escape(winName)}"]`);
    if (el) el.classList.add('unread');
  } else {
    w.term.scrollToBottom();
  }
}

function addWinTab(name) {
  if (document.querySelector(`.win-item[data-win="${CSS.escape(name)}"]`)) return;
  const el = document.createElement('div');
  el.className = 'win-item';
  el.dataset.win = name;
  el.textContent = name;
  el.onclick = () => switchWin(name);
  document.getElementById('win-list').appendChild(el);
}

// ── Event formatting ──────────────────────────────────────────────────────────

function formatEvent(e) {
  const t = ts(e.timestamp);
  switch (e.type) {
    case 'privmsg':
      return `${t} <${nc(e.nick)}> ${esc(e.text)}`;
    case 'action':
      return `${t} ${MAGENTA}** ${esc(e.nick)} ${esc(e.text)}${R}`;
    case 'join':
      return `${t} ${BGREEN}--> ${e.nick} (${esc(e.hostmask)}) has joined ${e.channel}${R}`;
    case 'part':
      return `${t} ${RED}<-- ${e.nick} (${esc(e.hostmask)}) has left ${e.channel}${e.reason ? ' (' + esc(e.reason) + ')' : ''}${R}`;
    case 'quit':
      return `${t} ${RED}<-- ${e.nick} (${esc(e.hostmask)}) has quit${e.reason ? ' (' + esc(e.reason) + ')' : ''}${R}`;
    case 'kick':
      return `${t} ${YELLOW}<-- ${e.kicked} was kicked by ${e.nick} (${esc(e.reason || '')})${R}`;
    case 'mode':
      return e.mode !== undefined
        ? `${t} ${YELLOW}-- Mode ${e.channel} [${esc(e.mode)}] by ${e.nick}${R}`
        : `${t} ${YELLOW}-- ${esc(e.text)}${R}`;
    case 'nick':
      return e.new_nick
        ? `${t} ${CYAN}-- ${e.nick} is now known as ${e.new_nick}${R}`
        : `${t} ${CYAN}-- ${esc(e.text)}${R}`;
    case 'topic':
      return `${t} ${YELLOW}-- ${e.nick} changed topic to: ${esc(e.topic || e.text)}${R}`;
    case 'notice':
      return `${t} ${CYAN}-${esc(e.nick || 'server')}- ${esc(e.text)}${R}`;
    case 'motd':
      return `${t} ${BLUE}${esc(e.text)}${R}`;
    case 'ctcp':
      return `${t} ${MAGENTA}[CTCP] ${esc(e.text)}${R}`;
    case 'connect':
      return `${t} ${BGREEN}*** ${esc(e.text)}${R}`;
    case 'disconnect':
      return `${t} ${BRED}*** ${esc(e.text)}${R}`;
    case 'server':
      return `${t} ${BLUE}${esc(e.text)}${R}`;
    case 'error':
      return `${t} ${BRED}!!! ${esc(e.text)}${R}`;
    case 'whois': {
      const d = e.whois || {};
      const lines = [
        `${t} ${CYAN}[${e.nick}] (${d.user}@${d.host}): ${esc(d.realname)}${R}`,
      ];
      if (d.server)   lines.push(`${ts(e.timestamp)} ${CYAN}[${e.nick}] server: ${esc(d.server)}${R}`);
      if (d.channels) lines.push(`${ts(e.timestamp)} ${CYAN}[${e.nick}] channels: ${esc(d.channels)}${R}`);
      if (d.idle)     lines.push(`${ts(e.timestamp)} ${CYAN}[${e.nick}] idle: ${d.idle}s${R}`);
      lines.push(`${ts(e.timestamp)} ${CYAN}[${e.nick}] End of WHOIS${R}`);
      return lines.join('\r\n');
    }
    default:
      return e.text ? `${t} ${esc(e.text)}` : null;
  }
}

// ── Names panel ───────────────────────────────────────────────────────────────

function renderNames(users) {
  const panel = document.getElementById('names-panel');
  panel.innerHTML = '';
  const ops = [], voices = [], rest = [];
  for (const [nick, info] of users) {
    if (info.prefix === '@') ops.push([nick, info]);
    else if (info.prefix === '+') voices.push([nick, info]);
    else rest.push([nick, info]);
  }
  const addSection = (label, list, cls) => {
    if (!list.length) return;
    const sec = document.createElement('div');
    sec.className = 'names-section';
    sec.textContent = `${label} (${list.length})`;
    panel.appendChild(sec);
    for (const [nick, info] of list) {
      const el = document.createElement('div');
      el.className = `name-entry ${cls}`;
      el.textContent = (info.prefix || '') + nick;
      el.dataset.nick = nick;
      el.oncontextmenu = (ev) => showCtxMenu(ev, nick, activeWin);
      el.ondblclick = () => { doAction('whois', nick, activeWin); };
      panel.appendChild(el);
    }
  };
  addSection('Ops', ops, 'name-op');
  addSection('Voice', voices, 'name-voice');
  addSection('Users', rest, '');
}

function updateUsers(channel, users) {
  const w = wins.get(channel);
  if (!w) return;
  w.users = new Map();
  for (const u of users) {
    w.users.set(u.nick.toLowerCase(), { prefix: u.prefix || '', hostmask: u.hostmask || '', nick: u.nick });
  }
  if (channel === activeWin) renderNames(w.users);
}

// ── Context menu ──────────────────────────────────────────────────────────────

function showCtxMenu(ev, nick, channel) {
  ev.preventDefault();
  ctxTarget = { nick, channel };
  const m = document.getElementById('ctx-menu');
  document.getElementById('ctx-nick-label').textContent = nick;
  m.style.display = 'block';
  const x = Math.min(ev.clientX, window.innerWidth  - m.offsetWidth  - 4);
  const y = Math.min(ev.clientY, window.innerHeight - m.offsetHeight - 4);
  m.style.left = x + 'px';
  m.style.top  = y + 'px';
}

function hideCtxMenu() {
  document.getElementById('ctx-menu').style.display = 'none';
}

function doAction(action, nick, channel) {
  if (action === 'whois') {
    socket.emit('irc_action', { action: 'whois', nick, channel });
    writeLine(channel, `${ts()} ${GRAY}-- /whois ${nick}${R}`);
    return;
  }
  let reason = '';
  if (action === 'kick' || action === 'kickban') {
    reason = prompt(`Reason for kicking ${nick}:`) || '';
  }
  socket.emit('irc_action', { action, nick, channel, reason });
}

// ── Input handling ────────────────────────────────────────────────────────────

function handleInput(raw) {
  const text = raw.trim();
  if (!text) return;
  inputHistory.unshift(text);
  histIdx = -1;
  // No local echo — IRC events (join, privmsg, action, server replies) come back and display
  socket.emit('send_message', { channel: activeWin, text });
}

function tabComplete(input) {
  const val   = input.value;
  const words = val.split(' ');
  const last  = words[words.length - 1];
  if (!last) return;
  const w     = wins.get(activeWin);
  if (!w) return;
  const nicks = Array.from(w.users.values()).map(u => u.nick);
  const match = nicks.find(n => n.toLowerCase().startsWith(last.toLowerCase()));
  if (match) {
    words[words.length - 1] = words.length === 1 ? match + ': ' : match + ' ';
    input.value = words.join(' ');
  }
}

// ── Socket.IO ─────────────────────────────────────────────────────────────────

function connectSocket() {
  socket = io({ transports: ['websocket', 'polling'] });

  socket.on('connect', () => {
    for (const name of wins.keys()) {
      socket.emit('subscribe', { channel: name });
    }
  });

  // Replay history from DB logs into the correct terminal
  socket.on('history', (data) => {
    const channel = data.channel;
    const lines   = data.lines || [];
    ensureWin(channel);
    const w = wins.get(channel);
    if (!w || !lines.length) return;
    w.term.writeln(`${GRAY}── history ──────────────────────────────────${R}`);
    for (const msg of lines) {
      const line = formatEvent(msg);
      if (line) w.term.writeln(line);
    }
    w.term.writeln(`${GRAY}── end of history ───────────────────────────${R}`);
    if (channel === activeWin) w.term.scrollToBottom();
  });

  socket.on('irc_event', (e) => {
    const target = e.channel || '*status*';
    if (!wins.has(target)) addWinTab(target);
    ensureWin(target);

    // Handle structural updates
    if (e.type === 'names_update') { updateUsers(target, e.users || []); return; }
    if (e.type === 'topic_update') {
      const w = wins.get(target);
      if (w) { w.topic = e.topic || ''; if (target === activeWin) document.getElementById('topic-bar').textContent = w.topic ? `${target}  —  ${w.topic}` : target; }
      return;
    }

    const line = formatEvent(e);
    if (line) writeLine(target, line);

    // Keep user list up to date for simple events
    const w = wins.get(target);
    if (w && e.nick) {
      if (e.type === 'join') {
        w.users.set(e.nick.toLowerCase(), { prefix: '', hostmask: e.hostmask || '', nick: e.nick });
        if (target === activeWin) renderNames(w.users);
      } else if (e.type === 'part' || e.type === 'kick') {
        const gone = e.type === 'kick' ? e.kicked : e.nick;
        w.users.delete((gone || '').toLowerCase());
        if (target === activeWin) renderNames(w.users);
      }
    }
    if (e.type === 'quit' && e.nick) {
      for (const [wname, ww] of wins) {
        if (ww.users.delete(e.nick.toLowerCase()) && wname === activeWin) renderNames(ww.users);
      }
    }
    if (e.type === 'nick' && e.nick && e.new_nick) {
      for (const [wname, ww] of wins) {
        const info = ww.users.get(e.nick.toLowerCase());
        if (info) {
          info.nick = e.new_nick;
          ww.users.delete(e.nick.toLowerCase());
          ww.users.set(e.new_nick.toLowerCase(), info);
          if (wname === activeWin) renderNames(ww.users);
        }
      }
    }
  });

  // Legacy handler for messages sent from the web itself
  socket.on('irc_message', (e) => {
    const target = e.channel || '*status*';
    ensureWin(target);
    const line = formatEvent(e);
    if (line) writeLine(target, line);
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

function initTerminal(channels, activeChannel) {
  // Create status window first
  ensureWin('*status*');
  addWinTab('*status*');

  for (const ch of channels) {
    ensureWin(ch);
    addWinTab(ch);
  }

  switchWin(activeChannel || (channels.length ? channels[0] : '*status*'));

  // Input
  const input = document.getElementById('irc-input');
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      const v = input.value;
      input.value = '';
      handleInput(v);
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      histIdx = Math.min(histIdx + 1, inputHistory.length - 1);
      input.value = inputHistory[histIdx] || '';
    } else if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      histIdx = Math.max(histIdx - 1, -1);
      input.value = histIdx < 0 ? '' : inputHistory[histIdx];
    } else if (ev.key === 'Tab') {
      ev.preventDefault();
      tabComplete(input);
    }
  });

  // Context menu
  document.querySelectorAll('#ctx-menu .ctx-item').forEach(el => {
    el.addEventListener('click', () => {
      doAction(el.dataset.action, ctxTarget.nick, ctxTarget.channel);
      hideCtxMenu();
    });
  });
  document.addEventListener('click', hideCtxMenu);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hideCtxMenu(); });

  // Resize
  window.addEventListener('resize', () => {
    const w = wins.get(activeWin);
    if (w) w.fit.fit();
  });

  // FitAddon measures character-cell size from the DOM at call time. If that
  // happens before the browser has finished font matching/layout, it locks in
  // the wrong cell width, so the terminal buffer's column count no longer
  // matches the eventually-rendered glyph width (bad wrapping, oversized
  // internal screen element). Re-fit every window once fonts have settled.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      for (const w of wins.values()) w.fit.fit();
      const w = wins.get(activeWin);
      if (w) w.term.scrollToBottom();
    });
  }

  connectSocket();
}
