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
dnf install -y openssl python3.12 python3.12-pip || dnf install -y openssl python3 python3-pip
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
  umask 077
  printf 'ERROR_ARCHIVER_WEB_SECRET=%s\n' "$(openssl rand -hex 32)" > "$RUNTIME_DIR/runtime.env"
  chown root:"$APP_GROUP" "$RUNTIME_DIR/runtime.env"
  chmod 0640 "$RUNTIME_DIR/runtime.env"
fi
systemctl daemon-reload
systemctl enable --now error-log-archiver.timer error-log-archiver-web.service
if command -v firewall-cmd >/dev/null 2>&1; then
  timeout 15 systemctl enable --now firewalld >/dev/null 2>&1 || true
  zone="$(timeout 15 firewall-cmd --get-active-zones 2>/dev/null | awk 'NR==1 {print $1}')"
  zone="${zone:-$(timeout 15 firewall-cmd --get-default-zone 2>/dev/null || printf public)}"
  timeout 15 firewall-cmd --zone="$zone" --permanent --add-service=https >/dev/null 2>&1 || true
  timeout 15 firewall-cmd --reload >/dev/null 2>&1 || true
fi
cat <<'NOTICE'
Installed. The job is disabled until OCI Vault Secret OCIDs are configured in the web console.
Open HTTPS on port 443 in both the host firewall and OCI NSG/security list.
NOTICE
echo "Use: systemctl status error-log-archiver-web error-log-archiver.timer"
