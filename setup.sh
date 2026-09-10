#!/usr/bin/env bash
# Install on Oracle Linux 9. Run as root from the checked-out application directory.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${ERROR_ARCHIVER_SERVICE_USER:-errorlogarchiver}"
APP_GROUP="${ERROR_ARCHIVER_SERVICE_GROUP:-$APP_USER}"
RUNTIME_DIR=/etc/error-log-archiver
STATE_DIR=/var/lib/error-log-archiver
[[ "$(id -u)" -eq 0 ]] || { echo 'Run setup.sh as root.' >&2; exit 1; }
source /etc/os-release
[[ "${PLATFORM_ID:-}" == "platform:el9" || "${VERSION_ID%%.*}" == "9" ]] || { echo 'This installer supports Linux/Oracle Linux 9.' >&2; exit 1; }
dnf install -y python3.12 python3.12-pip || dnf install -y python3 python3-pip
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$STATE_DIR" --shell /sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$STATE_DIR"
install -d -o root -g "$APP_GROUP" -m 0750 "$RUNTIME_DIR"
install -d -o root -g "$APP_GROUP" -m 0750 "$RUNTIME_DIR/tls"
if [[ ! -f "$RUNTIME_DIR/tls/server.key" || ! -f "$RUNTIME_DIR/tls/server.crt" ]]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes \
    -keyout "$RUNTIME_DIR/tls/server.key" -out "$RUNTIME_DIR/tls/server.crt" \
    -subj "/CN=$(hostname -f 2>/dev/null || hostname)"
  chown root:"$APP_GROUP" "$RUNTIME_DIR/tls/server.key" "$RUNTIME_DIR/tls/server.crt"
  chmod 0640 "$RUNTIME_DIR/tls/server.key"
  chmod 0644 "$RUNTIME_DIR/tls/server.crt"
fi
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
PYTHON_BIN="$(command -v python3.12 || command -v python3)"
runuser -u "$APP_USER" -- "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --upgrade pip
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
install -m 0644 "$APP_DIR/systemd/error-log-archiver.service" /etc/systemd/system/error-log-archiver.service
install -m 0644 "$APP_DIR/systemd/error-log-archiver.timer" /etc/systemd/system/error-log-archiver.timer
install -m 0644 "$APP_DIR/systemd/error-log-archiver-web.service" /etc/systemd/system/error-log-archiver-web.service
if [[ ! -f "$RUNTIME_DIR/runtime.env" ]]; then
  install -m 0640 -o root -g "$APP_GROUP" /dev/null "$RUNTIME_DIR/runtime.env"
  cat >&2 <<'NOTICE'
Create /etc/error-log-archiver/runtime.env with ERROR_ARCHIVER_WEB_PASSWORD and initial DB credentials,
then use the web console to manage all job settings. See README.md.
NOTICE
fi
systemctl daemon-reload
systemctl enable --now error-log-archiver.timer error-log-archiver-web.service
echo "Installed. Use: systemctl status error-log-archiver-web error-log-archiver.timer"
