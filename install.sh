#!/usr/bin/env bash
# StegoBot installer
# Usage: sudo bash install.sh
# Or just run as root to skip interactive prompts and use all defaults.
set -e

INSTALL_DIR=/opt/stegobot
VENV="$INSTALL_DIR/venv"
SERVICE_FILE="$INSTALL_DIR/stegobot.service"
NGINX_CONF="/etc/nginx/sites-available/stegobot"

echo "=== StegoBot Installer ==="

# ── Create system user ────────────────────────────────────────────────────────
if ! id stegobot &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin stegobot
  echo "Created system user 'stegobot'."
fi

# ── Python venv + deps ────────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "Python dependencies installed."

# ── Download socket.io client (for web terminal) ─────────────────────────────
SIOJS="$INSTALL_DIR/web/static/socket.io.min.js"
if [ ! -f "$SIOJS" ]; then
  curl -sL "https://cdn.socket.io/4.7.5/socket.io.min.js" -o "$SIOJS" || \
  python3 -c "
import urllib.request
urllib.request.urlretrieve(
  'https://cdn.socket.io/4.7.5/socket.io.min.js',
  '$SIOJS')
"
  echo "socket.io client downloaded."
fi

# ── Initialise database ────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/stegobot.db" ]; then
  if [ -t 0 ]; then
    echo ""
    echo "-- Initial bot configuration (press Enter to accept the default) --"
    read -rp "Bot nick [steg0saur]: " STEGOBOT_NICK
    read -rp "Alt nick [stegOsaur]: " STEGOBOT_ALTNICK
    read -rp "Admin email (for web UI login): " STEGOBOT_ADMIN_EMAIL
    read -rp "Admin hostmask (e.g. *!*@yourhost.example.com): " STEGOBOT_ADMIN_HOSTMASK
    read -rp "Channels to auto-join, comma separated (e.g. #chan1,#chan2): " STEGOBOT_CHANNELS
    read -rp "IRC servers, comma separated host[:port] (e.g. irc.libera.chat:6667,irc.efnet.org): " STEGOBOT_SERVERS
    read -rp "SMTP host for login emails (blank to configure later via web UI): " STEGOBOT_SMTP_HOST
    if [ -n "$STEGOBOT_SMTP_HOST" ]; then
      read -rp "SMTP username: " STEGOBOT_SMTP_USER
      read -rsp "SMTP password: " STEGOBOT_SMTP_PASS; echo
      read -rp "SMTP 'From' address: " STEGOBOT_SMTP_FROM
    fi
    export STEGOBOT_NICK STEGOBOT_ALTNICK STEGOBOT_ADMIN_EMAIL STEGOBOT_ADMIN_HOSTMASK \
           STEGOBOT_CHANNELS STEGOBOT_SERVERS STEGOBOT_SMTP_HOST STEGOBOT_SMTP_USER \
           STEGOBOT_SMTP_PASS STEGOBOT_SMTP_FROM
  else
    echo "Running non-interactively — initialising database with bare defaults."
    echo "  Configure nick/channels/servers/admin/SMTP later via the web config page."
  fi
  "$VENV/bin/python3" "$INSTALL_DIR/init_db.py"
  echo "Database initialised."
else
  echo "Database already exists — skipping init (run init_db.py manually to reset)."
fi

# ── File permissions ──────────────────────────────────────────────────────────
chown -R stegobot:stegobot "$INSTALL_DIR"
chmod -R 750 "$INSTALL_DIR"
chmod 640 "$INSTALL_DIR/stegobot.db" 2>/dev/null || true

# ── Systemd service ───────────────────────────────────────────────────────────
cp "$SERVICE_FILE" /etc/systemd/system/stegobot.service
systemctl daemon-reload
systemctl enable stegobot
systemctl restart stegobot
echo "Systemd service installed and started."

# ── Nginx vhost ───────────────────────────────────────────────────────────────
VHOST=""
if command -v nginx &>/dev/null; then
  if [ -t 0 ]; then
    read -rp "Set up an nginx vhost for the web UI? [y/N] " setup_vhost
    if [[ "$setup_vhost" =~ ^[Yy] ]]; then
      read -rp "Vhost hostname (e.g. bot.example.com): " VHOST
      if [ -z "$VHOST" ]; then
        echo "No hostname given — skipping nginx vhost setup."
      fi
    fi
  else
    echo "Nginx detected but running non-interactively — skipping vhost setup."
    echo "  Re-run install.sh interactively, or configure nginx.conf manually."
  fi

  if [ -n "$VHOST" ] && [ -d /etc/nginx/sites-available ]; then
    sed "s/__VHOST__/$VHOST/g" "$INSTALL_DIR/nginx.conf" > "$NGINX_CONF"
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/stegobot
    nginx -t && systemctl reload nginx
    echo "Nginx vhost configured for $VHOST."
    echo ""
    echo "  To enable HTTPS: certbot --nginx -d $VHOST"
  fi
fi

echo ""
echo "=== Installation complete ==="
echo "Logs:    journalctl -u stegobot -f"
if [ -n "$VHOST" ]; then
  echo "Web UI:  http://$VHOST  (or http://localhost:8080)"
else
  echo "Web UI:  http://localhost:8080"
fi
echo "Bot DB:  $INSTALL_DIR/stegobot.db"
