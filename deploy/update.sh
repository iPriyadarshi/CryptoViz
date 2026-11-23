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
SOCKET="/run/cryptoviz/gunicorn.sock"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run with sudo/root." >&2
    exit 1
fi

cd "$APP_DIR"

if [[ ! -d .git ]]; then
    echo "$APP_DIR is not a git checkout. Pull in your clone and re-run deploy/bootstrap.sh." >&2
    exit 1
fi

log "Fetching the latest code"
sudo -H -u "$APP_USER" git fetch --all --prune
sudo -H -u "$APP_USER" git reset --hard "@{u}"

log "Syncing dependencies"
bash "$APP_DIR/deploy/sync-env.sh"

log "Reinstalling systemd units"
install -m 644 "$APP_DIR/deploy/systemd/cryptoviz-web.service" \
        /etc/systemd/system/cryptoviz-web.service
install -m 644 "$APP_DIR/deploy/systemd/cryptoviz-worker.service" \
        /etc/systemd/system/cryptoviz-worker.service
systemctl daemon-reload

log "Restarting services"
systemctl restart cryptoviz-web.service
systemctl restart cryptoviz-worker.service

# Ask gunicorn directly: once HTTPS is on, plain localhost no longer reaches the
# site. A cold start on a small instance can take a while to bind.
for _ in {1..30}; do
    if curl -fsS --max-time 3 --unix-socket "$SOCKET" http://localhost/healthz >/dev/null 2>&1 \
        && systemctl is-active --quiet cryptoviz-worker.service; then
        log "Deploy complete - health check passed"
        exit 0
    fi
    sleep 2
done

echo "Health check FAILED. Recent logs:" >&2
journalctl -u cryptoviz-web -u cryptoviz-worker -n 40 --no-pager >&2
exit 1
