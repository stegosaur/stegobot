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

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function ts(iso) {
  let d;
  if (iso) {
    // The server sends naive UTC ISO timestamps (no trailing 'Z'/offset). Without
    // forcing that, `new Date()` parses them as local time and displayed times
    // end up skewed by the server/browser UTC offset.
    d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z');
  } else {
    d = new Date();
  }
  const mon  = MONTHS[d.getMonth()];
  const day  = String(d.getDate()).padStart(2, '0');
  const year = d.getFullYear();
  const time = d.toTimeString().slice(0, 8);
  return `${GRAY}${mon}/${day}/${year}-${time}${R}`;
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
//           users: Map(nick -> {prefix,hostmask}), topic: '',
//           activity: null|'text'|'msg'|'hilight', activityRank: 0-3 }
const wins = new Map();
let activeWin = '';
let socket;
let inputHistory = [];
let histIdx = -1;
let ctxTarget = { nick: '', channel: '' };
let myNick = '';
let tabState = null; // { start, stem, matches, idx } — nick-completion cycle state

const ACT_RANK = { text: 1, msg: 2, hilight: 3 };

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// IRC nick-char set, used to approximate word boundaries around a nick mention.
function mentionsMe(text) {
  if (!myNick || !text) return false;
  const re = new RegExp(
    "(^|[^A-Za-z0-9_\\-\\[\\]\\\\`^{}|])" + escapeRegExp(myNick) + "(?![A-Za-z0-9_\\-\\[\\]\\\\`^{}|])",
    'i'
  );
  return re.test(text);
}

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

  if (name === '*list*') {
    const el = document.createElement('div');
    el.className = 'term-pane list-pane';
    document.getElementById('term-container').appendChild(el);
    el.innerHTML =
      '<div class="list-status"></div>' +
      '<div class="list-wrap"><table class="list-table"><thead><tr>' +
      '<th data-col="channel">Channel</th>' +
      '<th data-col="topic">Topic</th>' +
      '<th data-col="users">Users</th>' +
      '</tr></thead><tbody></tbody></table></div>';
    const w = {
      kind: 'list', el, users: new Map(), topic: '', activity: null, activityRank: 0,
      data: [], sortCol: 'users', sortDir: 'desc', loading: false,
    };
    el.querySelectorAll('th').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (w.sortCol === col) w.sortDir = (w.sortDir === 'asc' ? 'desc' : 'asc');
        else { w.sortCol = col; w.sortDir = col === 'users' ? 'desc' : 'asc'; }
        renderListTable(w);
      });
    });
    wins.set(name, w);
    return w;
  }

  const el  = document.createElement('div');
  el.className = 'term-pane';
  document.getElementById('term-container').appendChild(el);

  const term = new Terminal(TERM_OPTS);
  const fit  = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);

  const w = { term, fit, el, users: new Map(), topic: '', activity: null, activityRank: 0 };
  wins.set(name, w);
  return w;
}

function renderListTable(w) {
  const tbody    = w.el.querySelector('tbody');
  const statusEl = w.el.querySelector('.list-status');
  statusEl.textContent = w.loading
    ? 'Loading channel list…'
    : `${w.data.length} channel${w.data.length === 1 ? '' : 's'}`;

  w.el.querySelectorAll('th').forEach(th => {
    th.classList.toggle('sorted', th.dataset.col === w.sortCol);
    th.dataset.dir = th.dataset.col === w.sortCol ? w.sortDir : '';
  });

  const rows = w.data.slice().sort((a, b) => {
    let av = a[w.sortCol], bv = b[w.sortCol];
    if (typeof av === 'string') { av = av.toLowerCase(); bv = bv.toLowerCase(); }
    if (av < bv) return w.sortDir === 'asc' ? -1 : 1;
    if (av > bv) return w.sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  tbody.innerHTML = '';
  for (const row of rows) {
    const tr = document.createElement('tr');
    const tdChan = document.createElement('td');
    tdChan.textContent = row.channel;
    tdChan.className = 'list-chan';
    tdChan.title = `/join ${row.channel}`;
    tdChan.addEventListener('click', () => {
      socket.emit('send_message', { channel: activeWin, text: `/join ${row.channel}` });
    });
    const tdTopic = document.createElement('td');
    tdTopic.textContent = row.topic;
    const tdUsers = document.createElement('td');
    tdUsers.textContent = row.users;
    tdUsers.className = 'list-users';
    tr.append(tdChan, tdTopic, tdUsers);
    tbody.appendChild(tr);
  }
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
  w.activity = null;
  w.activityRank = 0;
  activeWin = name;

  if (w.term) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (w.fit) w.fit.fit();
      w.term.scrollToBottom();
    }));
  }

  // Update UI
  document.getElementById('topic-bar').textContent = w.topic ? `${name}  —  ${w.topic}` : name;
  document.getElementById('input-prompt').textContent = `[${name}]`;
  document.querySelectorAll('.win-item').forEach(el => {
    el.classList.toggle('active', el.dataset.win === name);
    if (el.dataset.win === name) {
      el.classList.remove('unread', 'unread-msg', 'unread-mention');
    }
  });
  renderNames(w.users);
  updateTitle();
  document.getElementById('irc-input').focus();
}

function bumpActivity(w, winName, level) {
  const rank = ACT_RANK[level] || ACT_RANK.text;
  if (rank <= w.activityRank) return;
  w.activityRank = rank;
  w.activity = level;
  const el = document.querySelector(`.win-item[data-win="${CSS.escape(winName)}"]`);
  if (el) {
    el.classList.remove('unread', 'unread-msg', 'unread-mention');
    el.classList.add(level === 'hilight' ? 'unread-mention' : level === 'msg' ? 'unread-msg' : 'unread');
  }
  updateTitle();
}

function updateTitle() {
  let hi = 0, msg = 0;
  for (const w of wins.values()) {
    if (w.activity === 'hilight') hi++;
    else if (w.activity === 'msg') msg++;
  }
  document.title = hi ? `(!${hi}) StegoBot` : msg ? `(${msg}) StegoBot` : 'StegoBot';
}

function writeLine(winName, ansiText, level) {
  const w = ensureWin(winName);
  w.term.writeln(ansiText);
  if (winName !== activeWin) {
    bumpActivity(w, winName, level || 'text');
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

// Sender's current channel-status prefix, so chat lines read <@nick>/<+nick>/< nick>
// (space-padded on the plain case to keep the bracket column aligned) instead of
// dropping op/voice status entirely.
function prefixFor(channel, nick) {
  const w = wins.get((channel || '').toLowerCase());
  const info = w && w.users && w.users.get((nick || '').toLowerCase());
  const p = info && info.prefix;
  return p === '@' ? '@' : p === '+' ? '+' : ' ';
}

function formatEvent(e) {
  const t = ts(e.timestamp);
  switch (e.type) {
    case 'privmsg': {
      const hi = e.nick !== myNick && mentionsMe(e.text);
      const body = hi ? `${B}${BRED}${ircToAnsi(e.text)}${R}` : esc(e.text);
      return `${t} <${prefixFor(e.channel, e.nick)}${nc(e.nick)}> ${body}`;
    }
    case 'action': {
      const hi = e.nick !== myNick && mentionsMe(e.text);
      const body = hi ? `${B}${BRED}${ircToAnsi(e.text)}${R}` : esc(e.text);
      return `${t} ${MAGENTA}** ${esc(e.nick)} ${body}`;
    }
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

// ── Window context menu (close) ────────────────────────────────────────────────

let winCtxTarget = '';

function showWinCtxMenu(ev, name) {
  ev.preventDefault();
  if (name === '*status*') return; // status can't be closed — nothing to show
  winCtxTarget = name;
  const m = document.getElementById('win-ctx-menu');
  m.style.display = 'block';
  const x = Math.min(ev.clientX, window.innerWidth  - m.offsetWidth  - 4);
  const y = Math.min(ev.clientY, window.innerHeight - m.offsetHeight - 4);
  m.style.left = x + 'px';
  m.style.top  = y + 'px';
}

function hideWinCtxMenu() {
  document.getElementById('win-ctx-menu').style.display = 'none';
}

function closeWindow(name) {
  if (name === '*status*') return;
  if (name.startsWith('#')) {
    // Closing a channel window parts it; removeWinLocal happens when the
    // resulting self-part event comes back (see the irc_event handler), so
    // the tab doesn't disappear until the server actually confirms the part.
    socket.emit('send_message', { channel: name, text: '/part' });
  } else {
    removeWinLocal(name);
  }
}

function removeWinLocal(name) {
  const w = wins.get(name);
  if (!w) return;
  if (w.term) w.term.dispose();
  if (w.el) w.el.remove();
  wins.delete(name);
  const tab = document.querySelector(`.win-item[data-win="${CSS.escape(name)}"]`);
  if (tab) tab.remove();
  if (activeWin === name) switchWin('*status*');
  updateTitle();
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
  if (/^\/clear\b/i.test(text)) {
    const w = wins.get(activeWin);
    if (w && w.term) w.term.clear();
    return;
  }
  // /query is a client-only concept (irssi doesn't send it to the server): it
  // just opens/focuses a window for that nick, optionally with an initial message.
  const queryMatch = text.match(/^\/query\s+(\S+)(?:\s+([\s\S]+))?$/i);
  if (queryMatch) {
    const target = queryMatch[1].toLowerCase();
    const initialMsg = queryMatch[2];
    const isNew = !wins.has(target);
    ensureWin(target);
    addWinTab(target);
    if (isNew) socket.emit('subscribe', { channel: target });
    switchWin(target);
    if (initialMsg) socket.emit('send_message', { channel: target, text: initialMsg });
    return;
  }
  // No local echo — IRC events (join, privmsg, action, server replies) come back and display
  socket.emit('send_message', { channel: activeWin, text });
}

function tabComplete(input) {
  if (tabState) {
    // Cycling: strip the previously inserted "match + separator" back off.
    input.value = input.value.slice(0, tabState.start);
  } else {
    const val   = input.value;
    const start = val.lastIndexOf(' ') + 1;
    const stem  = val.slice(start);
    if (!stem) return;
    const w = wins.get(activeWin);
    if (!w) return;
    const nicks   = Array.from(w.users.values()).map(u => u.nick);
    const matches = nicks.filter(n => n.toLowerCase().startsWith(stem.toLowerCase()));
    if (!matches.length) return;
    tabState = { start, stem, matches, idx: -1 };
  }
  tabState.idx = (tabState.idx + 1) % tabState.matches.length;
  const match = tabState.matches[tabState.idx];
  const sep   = tabState.start === 0 ? ': ' : ' ';
  input.value += match + sep;
}

function cycleWindow(dir) {
  const items  = Array.from(document.querySelectorAll('#win-list .win-item'));
  const curIdx = items.findIndex(el => el.dataset.win === activeWin);
  if (curIdx === -1 || !items.length) return;
  const next = (curIdx + dir + items.length) % items.length;
  switchWin(items[next].dataset.win);
}

function switchWinByIndex(n) {
  const items = document.querySelectorAll('#win-list .win-item');
  if (items[n]) switchWin(items[n].dataset.win);
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
    if (!w || !w.term || !lines.length) return;
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

    // Our own part (whether from the window-close menu or a typed /part)
    // closes the window — checked first so the generic addWinTab below never
    // gets a chance to resurrect the tab we're removing.
    if (e.type === 'part' && e.nick === myNick) {
      removeWinLocal(target);
      return;
    }

    if (!wins.has(target)) {
      // First time we've seen this window this session (a new query/channel
      // discovered mid-session) — pull its scrollback, same as the windows
      // that were subscribed at connect time.
      addWinTab(target);
      socket.emit('subscribe', { channel: target });
    }
    ensureWin(target);

    // Handle structural updates
    if (e.type === 'names_update') { updateUsers(target, e.users || []); return; }
    if (e.type === 'list_start') {
      const w = wins.get(target);
      if (w) { w.data = []; w.loading = true; renderListTable(w); }
      switchWin(target);
      return;
    }
    if (e.type === 'list_result') {
      const w = wins.get(target);
      if (w) { w.data = e.channels || []; w.loading = false; renderListTable(w); }
      return;
    }
    if (e.type === 'topic_update') {
      const w = wins.get(target);
      if (w) { w.topic = e.topic || ''; if (target === activeWin) document.getElementById('topic-bar').textContent = w.topic ? `${target}  —  ${w.topic}` : target; }
      return;
    }

    const line = formatEvent(e);
    if (line) {
      let level = 'text';
      if (e.type === 'privmsg' || e.type === 'action' || e.type === 'notice') {
        // Any message in a query window (not a #channel) is inherently addressed
        // to you — treat it as top priority even if it doesn't literally contain
        // your nick as text.
        const isQuery = target !== '*status*' && target !== '*list*' && !target.startsWith('#');
        level = (isQuery || (e.nick !== myNick && mentionsMe(e.text))) ? 'hilight' : 'msg';
      }
      writeLine(target, line, level);
    }

    if (e.type === 'nick' && e.nick === myNick && e.new_nick) {
      myNick = e.new_nick;
    }

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

function initTerminal(channels, activeChannel, nick) {
  myNick = nick || '';

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
    if (ev.key !== 'Tab') tabState = null;

    if (ev.altKey) {
      // Alt+1..9, Alt+0 (=10th) jump to a window by position; Alt+Up/Down cycle —
      // both are irssi's default window-switching bindings.
      if (ev.key >= '1' && ev.key <= '9') {
        ev.preventDefault();
        switchWinByIndex(ev.key.charCodeAt(0) - '1'.charCodeAt(0));
        return;
      } else if (ev.key === '0') {
        ev.preventDefault();
        switchWinByIndex(9);
        return;
      } else if (ev.key === 'ArrowUp' || ev.key === 'ArrowDown') {
        ev.preventDefault();
        cycleWindow(ev.key === 'ArrowDown' ? 1 : -1);
        return;
      }
    }

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
    } else if (ev.key === 'PageUp') {
      ev.preventDefault();
      const w = wins.get(activeWin);
      if (w && w.term) w.term.scrollLines(-(w.term.rows - 2));
    } else if (ev.key === 'PageDown') {
      ev.preventDefault();
      const w = wins.get(activeWin);
      if (w && w.term) w.term.scrollLines(w.term.rows - 2);
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

  // Window-list context menu (close window) — delegated so it covers both the
  // server-rendered tabs and ones added later by addWinTab.
  document.getElementById('win-list').addEventListener('contextmenu', (ev) => {
    const item = ev.target.closest('.win-item');
    if (!item) return;
    showWinCtxMenu(ev, item.dataset.win);
  });
  document.querySelectorAll('#win-ctx-menu .ctx-item').forEach(el => {
    el.addEventListener('click', () => {
      if (el.dataset.action === 'close') closeWindow(winCtxTarget);
      hideWinCtxMenu();
    });
  });
  document.addEventListener('click', hideWinCtxMenu);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hideWinCtxMenu(); });

  // Resize: all panes are absolutely positioned to fill #term-container, so a
  // single observer on the container covers window resizes, sidebar/layout
  // changes, and browser zoom alike. This replaces relying on the DOM
  // `resize` event alone — that event doesn't fire for ordinary layout
  // settling (fonts loading, flex children reaching final size on first
  // paint), only for actual window resizes. Since browser zoom (ctrl+/-)
  // *does* fire a `resize` event, that was previously the only thing that
  // ever forced a corrective re-fit, which is why the terminal appeared to
  // only show a partial scrollback until the user zoomed in or out.
  const fitActive = () => {
    const w = wins.get(activeWin);
    if (w && w.fit) {
      try { w.fit.fit(); } catch (err) { /* container not laid out yet */ }
    }
  };
  if (window.ResizeObserver) {
    new ResizeObserver(fitActive).observe(document.getElementById('term-container'));
  } else {
    window.addEventListener('resize', fitActive);
  }

  // FitAddon measures character-cell size from the DOM at call time. If that
  // happens before the browser has finished font matching/layout, it locks in
  // the wrong cell width, so the terminal buffer's column count no longer
  // matches the eventually-rendered glyph width (bad wrapping, oversized
  // internal screen element). Re-fit every window once fonts have settled.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      for (const w of wins.values()) { if (w.fit) w.fit.fit(); }
      const w = wins.get(activeWin);
      if (w && w.term) w.term.scrollToBottom();
    });
  }

  connectSocket();
}
