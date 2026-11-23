#!/usr/bin/env bash
#
# Install the locked Python environment for the service user.
#
# Python itself goes under the application directory: the systemd units set
# ProtectHome, so an interpreter in uv's default location under /home could not
# be executed by the services. A .venv built against one is rebuilt.
#
# Called by bootstrap.sh and update.sh.

set -euo pipefail

APP_USER="cryptoviz"
APP_DIR="/opt/cryptoviz"
UV_BIN="/usr/local/bin/uv"

cd "$APP_DIR"

if [[ -e .venv/bin/python && "$(readlink -f .venv/bin/python)" != "$APP_DIR/.python/"* ]]; then
    echo "Rebuilding .venv against the Python in $APP_DIR/.python"
    rm -rf .venv
fi

# --frozen installs exactly what uv.lock pins. Bytecode is compiled here because
# the sandboxed services cannot write it, and would otherwise recompile on every
# start.
sudo -u "$APP_USER" env \
     "PATH=/usr/local/bin:$PATH" \
     HOME="/home/$APP_USER" \
     UV_PYTHON_INSTALL_DIR="$APP_DIR/.python" \
     UV_PYTHON_PREFERENCE=only-managed \
     "$UV_BIN" sync --frozen --no-dev --compile-bytecode
