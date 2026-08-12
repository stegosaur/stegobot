Markdown

# Python IRC Bot Requirements & Specification

## 1. Overview & Setup
* **Target Installation Path:** `/opt/stegobot`
* **Bot Executable:** Located in `/root` (or symlinked/installed via setup script)
* **Framework & Runtime:** Latest compatible Python version utilizing the [Sopel IRC Framework](https://github.com/sopel-irc/sopel).
* **System Service:** Configured as a `systemd` service for daemon management.
* **CTCP Version Response:** Returns an `irssi` client identifier when queried.
* **Logging & Maintenance:**
  * Logs all activity per channel in `/opt/stegobot/logs/`.
  * Automated nightly log rotation and compression.
* **Installation Script:** Interactive installer asking for initial configurations (Admin hostmask, channels, servers, nick, altNick) with defaults pre-filled based on the database spec below.

---

## 2. Database Architecture (SQLite3)

The bot uses SQLite3 for persistent storage. Configuration updates in the database take effect immediately without requiring a bot process rehash or restart.

### Default Database Records
* **Admin Hostmask:** `*!*@stegosaur.org`
* **Default Nick:** `steg0saur`
* **Alternate Nick (`altNick`):** `stegOsaur`

### Pre-populated Tables & Schemas

#### `config`
Stores main operational parameters.
* `nick`: Default `steg0saur`
* `altNick`: Default `stegOsaur`

#### `users`
Stores user permissions and hostmask matching.
* `hostmask`: e.g., `*!*@stegosaur.org`
* `role`: `admin` | `peon`

#### `servers`
Ordered list of IRC servers. On startup, the bot attempts connections sequentially. Upon a successful connection, the connected server rotates to the bottom of the list.
* **Default List:**
  1. `irc.prison.net`
  2. `irc.colosolutions.net`
  3. `irc.choopa.net`
  4. `irc.homelein.no`
  5. `irc.du.se`
  6. `irc.swepipe.se`
  7. `irc.mzima.net`
  8. `irc.efnet.nl`

#### `channels`
Tracks channel configurations and runtime metadata.
* **Columns:** `channel_name`, `currentTopic`, `first_joined`, `last_rejoin`, `num_users`
* **Default Auto-Join Channels:**
  * `#fmc`
  * `#predators_lair`
  * `#sp3`
  * `#outback`
  * `#god`
  * `#terror`

---

## 3. Bot Command Interface

Commands are accepted via **Public Channel** (`<bot_nick>: <command>`) or **Private Message** (`<command>`). Permissions are strictly enforced based on the `users` table:
* **Admin:** Access to all commands.
* **Peon:** Restricted to `op me`, `join`, and `leave`.
* **Unauthorized Users:** Unrecognized commands or unauthorized access attempts fail silently without response.

| # | Command Syntax | Description | Permission Level |
| :--- | :--- | :--- | :--- |
| **1** | `op me` *(In channel)*<br>`op me <#channel>` *(In PM)* | Op the requesting user in the specified or current channel. | `admin`, `peon` |
| **2** | `adduser <nick> <peon\|admin>` | Executes a `/WHOIS <nick>` to resolve hostmask, then saves the user and role to the database. | `admin` |
| **3** | `join <#channel>` | Directs the bot to join the target channel. | `admin`, `peon` |
| **4** | `leave` *(In channel)*<br>`leave <#channel>` *(In PM)* | Directs the bot to leave the target/current channel. | `admin`, `peon` |
| **5** | `nick <new_nick>` | Updates the bot's current nickname and modifies the `config` table. If the nick is taken, returns: `"the nick <requested nickname> is already in use"`. | `admin` |
| **6** | `query <SQL_QUERY>` | Executes raw SQL queries on the SQLite3 database and outputs results to the requesting context. | `admin` |
| **7** | `server <irc.server.com>` | Immediately disconnects from the current server and connects to the specified target server. | `admin` |
| **8** | `addserver <irc.server.com>`<br>`delserver <irc.server.com>` | Adds or removes server addresses from the `servers` table without breaking current connection. | `admin` |

---

## 4. Web Interface & Nginx Integration

* **Domain Endpoint:** `zappa.blacknapkins.org`
* **Web Server:** Embedded HTTP server managed by the bot and proxied via Nginx vhost.
* **Web Pages:**
  1. **Configuration Dashboard:** Unified view to manage all database configurations, servers, users, and channels via browser.
  2. **Web IRC Terminal:** Full-featured embedded web IRC client providing direct interaction as the bot.
