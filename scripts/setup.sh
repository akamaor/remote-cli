#!/usr/bin/env bash
# =============================================================================
# setup.sh — One-shot provisioning for Secure Remote Chat CLI
#
# Run as root from the project root:
#   sudo bash scripts/setup.sh
#
# What this does:
#   1. Creates the restricted 'chatcli' system user
#   2. Deploys the application to /opt/remote-cli
#   3. Creates and secures the log directory
#   4. Sets up a Python virtual environment
#   5. Installs the systemd service (but does NOT start it)
#   6. Installs the sudoers allowlist for chatcli
#
# After running this script:
#   1. Edit /opt/remote-cli/.env with your bot token and user IDs
#   2. sudo systemctl start remote-cli
# =============================================================================

set -euo pipefail

APP_DIR="/opt/remote-cli"
LOG_DIR="/var/log/remote-cli"
SERVICE_USER="chatcli"
SERVICE_FILE="systemd/remote-cli.service"
SUDOERS_FILE="scripts/sudoers_chatcli"

# ---- Preflight ----
if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] This script must be run as root." >&2
    exit 1
fi

if [[ ! -f "requirements.txt" ]]; then
    echo "[ERROR] Run this script from the project root directory." >&2
    exit 1
fi

echo "[1/7] Creating restricted system user '${SERVICE_USER}'..."
if id "${SERVICE_USER}" &>/dev/null; then
    echo "      User '${SERVICE_USER}' already exists — skipping."
else
    useradd \
        --system \
        --shell /sbin/nologin \
        --home-dir "${APP_DIR}" \
        --no-create-home \
        --comment "Secure Remote Chat CLI service account" \
        "${SERVICE_USER}"
    echo "      Created user '${SERVICE_USER}'."
fi

echo "[2/7] Deploying application to ${APP_DIR}..."
mkdir -p "${APP_DIR}"
rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.env' \
    ./ "${APP_DIR}/"

echo "[3/7] Setting ownership and permissions..."
chown -R root:root "${APP_DIR}"
# The service user needs read+execute on the app, but must not be able to modify it
chmod -R o-rwx "${APP_DIR}"
chgrp -R "${SERVICE_USER}" "${APP_DIR}"
chmod -R g+rX "${APP_DIR}"

# The .env file holds secrets — restrict to chatcli read-only
if [[ ! -f "${APP_DIR}/.env" ]]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    echo ""
    echo "      *** IMPORTANT: Edit ${APP_DIR}/.env before starting the service! ***"
    echo ""
fi
chown root:"${SERVICE_USER}" "${APP_DIR}/.env"
chmod 640 "${APP_DIR}/.env"

echo "[4/7] Creating log directory ${LOG_DIR}..."
mkdir -p "${LOG_DIR}"
chown "${SERVICE_USER}":"${SERVICE_USER}" "${LOG_DIR}"
chmod 750 "${LOG_DIR}"

echo "[5/7] Setting up Python virtual environment..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
chown -R root:"${SERVICE_USER}" "${APP_DIR}/venv"
chmod -R g+rX "${APP_DIR}/venv"

echo "[6/7] Installing systemd service..."
cp "${APP_DIR}/${SERVICE_FILE}" /etc/systemd/system/remote-cli.service
chmod 644 /etc/systemd/system/remote-cli.service
systemctl daemon-reload
systemctl enable remote-cli
echo "      Service enabled. Use 'systemctl start remote-cli' to start."

echo "[7/7] Installing sudoers allowlist for '${SERVICE_USER}'..."
if [[ -f "${SUDOERS_FILE}" ]]; then
    visudo -c -f "${SUDOERS_FILE}" && \
    cp "${SUDOERS_FILE}" /etc/sudoers.d/chatcli && \
    chmod 440 /etc/sudoers.d/chatcli
    echo "      Sudoers allowlist installed at /etc/sudoers.d/chatcli"
else
    echo "      [SKIP] ${SUDOERS_FILE} not found — skipping sudoers install."
    echo "      See scripts/sudoers_chatcli.example for the template."
fi

echo ""
echo "====================================================="
echo " Setup complete!"
echo "====================================================="
echo ""
echo " Next steps:"
echo "   1. Edit the bot token and allowed user IDs:"
echo "      nano ${APP_DIR}/.env"
echo ""
echo "   2. (Optional) Customize the sudo allowlist:"
echo "      visudo -f /etc/sudoers.d/chatcli"
echo ""
echo "   3. Start the service:"
echo "      systemctl start remote-cli"
echo ""
echo "   4. Watch the logs:"
echo "      journalctl -u remote-cli -f"
echo "      tail -f ${LOG_DIR}/audit.log"
echo ""
