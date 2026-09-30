#!/usr/bin/env bash
#
# One-time provisioning for a fresh EC2 instance.
#
# Installs system packages, uv and nginx; creates the service user, the
# application directory and the database directory; installs the Python
# environment and the systemd units; and starts everything.
#
# Safe to re-run: every step checks its own state first.
#
# Usage (as a user with sudo, from a checkout of the repository):
#     sudo bash deploy/bootstrap.sh

set -euo pipefail

APP_USER="cryptoviz"
APP_DIR="/opt/cryptoviz"
DATA_DIR="/var/lib/cryptoviz"
NLTK_DIR="/usr/local/share/nltk_data"
UV_BIN="/usr/local/bin/uv"

# Resolve the repository this script was run from, so the script works whether
# the checkout is already at APP_DIR or somewhere else.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run with sudo/root." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
log "Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq nginx curl ca-certificates rsync
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q nginx curl ca-certificates rsync
else
    echo "Unsupported distribution: need apt-get or dnf." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
if [[ ! -x "$UV_BIN" ]]; then
    log "Installing uv"
    # Install to a system-wide location so the service user can run it.
    curl -LsSf https://astral.sh/uv/install.sh | \
        env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
else
    log "uv already installed ($("$UV_BIN" --version))"
fi

# ---------------------------------------------------------------------------
# Service user and directories
# ---------------------------------------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
    log "Creating service user: $APP_USER"
    useradd --system --create-home --home-dir "/home/$APP_USER" \
            --shell /usr/sbin/nologin "$APP_USER"
else
    log "Service user $APP_USER already exists"
fi

log "Preparing directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$NLTK_DIR"

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
    log "Copying application code to $APP_DIR"
    rsync -a --delete \
          --exclude '.git' \
          --exclude '.venv' \
          --exclude '__pycache__' \
          --exclude '*.db' \
          "$REPO_DIR/" "$APP_DIR/"
else
    log "Already running from $APP_DIR"
fi

# The .env file holds configuration; seed it from the example on first run.
if [[ ! -f "$APP_DIR/.env" ]]; then
    log "Creating $APP_DIR/.env from .env.example"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    # Point the database at the durable data directory.
    sed -i "s#^CRYPTOVIZ_DB_PATH=.*#CRYPTOVIZ_DB_PATH=$DATA_DIR/crypto.db#" \
        "$APP_DIR/.env"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod 640 "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
log "Installing the Python environment with uv"
# --frozen installs exactly what uv.lock pins; the deployed environment is then
# identical to the one the lockfile was tested against.
cd "$APP_DIR"
sudo -u "$APP_USER" env "PATH=/usr/local/bin:$PATH" HOME="/home/$APP_USER" \
     "$UV_BIN" sync --frozen --no-dev

# ---------------------------------------------------------------------------
# NLTK data
# ---------------------------------------------------------------------------
log "Installing the VADER sentiment lexicon"
# Downloaded once, system-wide, so neither service reaches for the network at
# startup. NLTK_DATA in the unit files points here.
"$APP_DIR/.venv/bin/python" -m nltk.downloader -d "$NLTK_DIR" vader_lexicon
chmod -R a+rX "$NLTK_DIR"

# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------
log "Installing systemd units"
install -m 644 "$APP_DIR/deploy/systemd/cryptoviz-web.service" \
        /etc/systemd/system/cryptoviz-web.service
install -m 644 "$APP_DIR/deploy/systemd/cryptoviz-worker.service" \
        /etc/systemd/system/cryptoviz-worker.service
systemctl daemon-reload

# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------
log "Installing the nginx site"
if [[ -d /etc/nginx/sites-available ]]; then
    install -m 644 "$APP_DIR/deploy/nginx/cryptoviz.conf" \
            /etc/nginx/sites-available/cryptoviz
    ln -sf /etc/nginx/sites-available/cryptoviz \
           /etc/nginx/sites-enabled/cryptoviz
    # The default site also listens on :80 and would shadow ours.
    rm -f /etc/nginx/sites-enabled/default
else
    install -m 644 "$APP_DIR/deploy/nginx/cryptoviz.conf" \
            /etc/nginx/conf.d/cryptoviz.conf
fi

# nginx must be able to traverse into the static directories.
chmod o+x /opt/cryptoviz /opt/cryptoviz/src /opt/cryptoviz/src/cryptoviz

nginx -t

# ---------------------------------------------------------------------------
# Start everything
# ---------------------------------------------------------------------------
log "Starting services"
systemctl enable --now cryptoviz-web.service
systemctl enable --now cryptoviz-worker.service
systemctl restart nginx
systemctl enable nginx

log "Bootstrap complete"
cat <<EOF

  Web:     systemctl status cryptoviz-web
  Worker:  systemctl status cryptoviz-worker
  Logs:    journalctl -u cryptoviz-web -u cryptoviz-worker -f
  Health:  curl -s localhost/healthz

  The worker collects its first data within a few minutes. To backfill 31 days
  of price history now (takes several minutes, rate-limited by CoinGecko):

      sudo -u $APP_USER $APP_DIR/.venv/bin/cryptoviz-backfill

EOF
