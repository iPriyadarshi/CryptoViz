#!/usr/bin/env bash
#
# Deploy a new version onto an already-bootstrapped instance.
#
# Pulls the latest code, re-syncs dependencies and restarts both services.
# The database is untouched.
#
# Usage:
#     sudo bash /opt/cryptoviz/deploy/update.sh

set -euo pipefail

APP_USER="cryptoviz"
APP_DIR="/opt/cryptoviz"
UV_BIN="/usr/local/bin/uv"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run with sudo/root." >&2
    exit 1
fi

cd "$APP_DIR"

if [[ -d .git ]]; then
    log "Fetching the latest code"
    sudo -u "$APP_USER" git fetch --all --prune
    sudo -u "$APP_USER" git reset --hard "@{u}"
else
    log "Not a git checkout; skipping the pull (copy the code in yourself)"
fi

log "Syncing dependencies"
sudo -u "$APP_USER" env "PATH=/usr/local/bin:$PATH" HOME="/home/$APP_USER" \
     "$UV_BIN" sync --frozen --no-dev

log "Reinstalling systemd units"
install -m 644 "$APP_DIR/deploy/systemd/cryptoviz-web.service" \
        /etc/systemd/system/cryptoviz-web.service
install -m 644 "$APP_DIR/deploy/systemd/cryptoviz-worker.service" \
        /etc/systemd/system/cryptoviz-worker.service
systemctl daemon-reload

log "Restarting services"
systemctl restart cryptoviz-web.service
systemctl restart cryptoviz-worker.service

# Give gunicorn a moment to bind before checking.
sleep 2
if curl -fsS localhost/healthz >/dev/null; then
    log "Deploy complete - health check passed"
else
    echo "Health check FAILED. Recent logs:" >&2
    journalctl -u cryptoviz-web -n 40 --no-pager >&2
    exit 1
fi
