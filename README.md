# StegoBot

Python IRC bot with a Flask/xterm.js web UI. Runs as a systemd service (`stegobot.service`). Web UI on port 8080.

---

## IRC Commands

Commands are addressed to the bot in a channel (`botnick: cmd`) or sent as a private message (`/msg botnick cmd`).

### Public commands (no auth required)

| Command | Example | Description |
|---|---|---|
| `prompt <text>` | `<bot nickname>: prompt what is tcp/ip?` | Ask Gemini AI a question |
| `ai <text>` | `<bot nickname>: ai explain tcp/ip` | Alias for `prompt` |
| `channelprompt <text>` | `<bot nickname>: channelprompt summarize the last 24h` | Ask Gemini about this channel's logs (feeds up to 2 MB of log history as context) |
| `stock <ticker>` | `<bot nickname>: stock AAPL` | Stock quote — price, change, range, cap, P/E, earnings, dividend |
| `quote <ticker>` | `<bot nickname>: quote TSLA` | Alias for `stock` |
| `urbandictionary <word>` | `<bot nickname>: urbandictionary rizz` | Urban Dictionary top definition |
| `ud <word>` | `<bot nickname>: ud rizz` | Alias for `urbandictionary` |
| `define <word>` | `<bot nickname>: define serendipity` | Dictionary definition (dictionaryapi.dev, up to 3 defs) |
| `def <word>` | `<bot nickname>: def serendipity` | Alias for `define` |

### Peon + Admin commands

| Command | Example | Description |
|---|---|---|
| `op me` | `<bot nickname>: op me` | Bot gives you `+o` in the current channel |
| `join <channel>` | `<bot nickname>: join #chat` | Bot joins a channel and saves it to DB |
| `leave` | `<bot nickname>: leave` | Bot parts the current channel and removes it from DB |

### Admin-only commands

| Command | Example | Description |
|---|---|---|
| `adduser <nick> <level>` | `<bot nickname>: adduser john peon` | WHOISes nick and adds their hostmask to the user DB (levels: `peon`, `admin`) |
| `nick <newnick>` | `<bot nickname>: nick newname` | Change the bot's nick and save to config |
| `query <sql>` | `<bot nickname>: query SELECT * FROM users` | Run a raw SQL query against the bot's SQLite DB (results in IRC, 10 row cap) |
| `server <host> [port]` | `<bot nickname>: server irc.libera.chat 6667` | Reconnect to a different IRC server |
| `addserver <host> [port]` | `<bot nickname>: addserver irc.libera.chat` | Add a server to the DB server list |
| `delserver <host>` | `<bot nickname>: delserver irc.libera.chat` | Remove a server from the DB server list |

---

## Web UI Slash Commands

Typed in the terminal input (prefix with `/`). Always operate on the currently active channel unless a target is specified.

| Command | Example | Description |
|---|---|---|
| `/join <channel>` | `/join #chat` | Join a channel |
| `/part [channel]` | `/part` | Part the current channel |
| `/leave [channel]` | `/leave #chat` | Alias for `/part` |
| `/nick <newnick>` | `/nick newname` | Change nick |
| `/msg <target> <text>` | `/msg john hey` | Send a private message |
| `/me <text>` | `/me waves` | Send a CTCP ACTION in current channel |
| `/quit [message]` | `/quit later` | Disconnect from IRC |
| `/kick <nick> [reason]` | `/kick john spam` | Kick nick from current channel |
| `/ban <nick>` | `/ban john` | Ban nick's host from current channel (`*!*@host`) |
| `/unban <mask>` | `/unban *!*@host` | Remove a ban |
| `/op <nick>` | `/op john` | Give `+o` to nick |
| `/deop <nick>` | `/deop john` | Remove `+o` from nick |
| `/voice <nick>` | `/voice john` | Give `+v` to nick |
| `/devoice <nick>` | `/devoice john` | Remove `+v` from nick |
| `/topic <text>` | `/topic welcome to #chat` | Set channel topic |
| `/whois <nick>` | `/whois john` | WHOIS a nick (result shown in channel) |
| `/mode <mode> [arg]` | `/mode +m` | Set a channel mode |
| `/raw <line>` | `/raw PRIVMSG #chan :hi` | Send a raw IRC line |
| `/<anything else>` | `/stats p` | Unknown slash commands are sent as raw IRC |

---

## Plugins

Drop a `.py` file into `plugins/` and it hot-reloads on next command — no restart needed.

### Plugin contract

```python
COMMANDS = ['cmd1', 'cmd2']   # command names (lowercase)
PUBLIC   = True               # if True, works without auth; False = peon+ only

def handle(cmd, args, ctx):
    # ctx keys: nick, channel, public, level, reply (callable), conn
    ctx['reply']('hello')
    return True               # return True = handled; None/False = skip
```

---

## Logs

Channel logs: `/opt/stegobot/logs/{channel}_{date}.log`
Compressed on midnight rollover: `{channel}_{date}.log.gz`

View live: `journalctl -u stegobot -f`

---

## Service management

```bash
systemctl status stegobot
systemctl restart stegobot
systemctl stop stegobot
journalctl -u stegobot -n 100
```
