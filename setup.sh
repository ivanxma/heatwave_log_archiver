#!/usr/bin/env bash
# Install on Oracle Linux 9. Run as root from the checked-out application directory.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${ERROR_ARCHIVER_SERVICE_USER:-errorlogarchiver}"
APP_GROUP="${ERROR_ARCHIVER_SERVICE_GROUP:-$APP_USER}"
RUNTIME_DIR=/etc/error-log-archiver
STATE_DIR=/var/lib/error-log-archiver
CONTROL_PROFILE="${ERROR_ARCHIVER_CONTROL_PROFILE:-}"
CONTROL_SCHEMA="${ERROR_ARCHIVER_CONTROL_SCHEMA:-archive_control}"
CONTROL_USER="${ERROR_ARCHIVER_CONTROL_USER:-}"
CONTROL_SECRET="${ERROR_ARCHIVER_CONTROL_SECRET_OCID:-}"
[[ "$(id -u)" -eq 0 ]] || { echo 'Run setup.sh as root.' >&2; exit 1; }
cd "$APP_DIR"
source /etc/os-release
[[ "${PLATFORM_ID:-}" == "platform:el9" || "${VERSION_ID%%.*}" == "9" ]] || { echo 'This installer supports Linux/Oracle Linux 9.' >&2; exit 1; }
if [[ -n "$CONTROL_PROFILE$CONTROL_USER$CONTROL_SECRET" ]]; then
  [[ -n "$CONTROL_PROFILE" && -n "$CONTROL_USER" && "$CONTROL_SECRET" == ocid1.vaultsecret.* ]] || {
    echo 'Set ERROR_ARCHIVER_CONTROL_PROFILE, ERROR_ARCHIVER_CONTROL_USER and a valid ERROR_ARCHIVER_CONTROL_SECRET_OCID together.' >&2
    exit 1
  }
fi
# Stop new executions before replacing dependencies or moving control data.
systemctl stop error-log-archiver.timer 2>/dev/null || true
if systemctl is-active --quiet error-log-archiver.service; then
  echo 'An archive cycle is running. Wait for it to finish, then rerun setup. The timer has been stopped.' >&2
  exit 1
fi
systemctl stop error-log-archiver-web.service 2>/dev/null || true
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
PYTHON_BIN="$(command -v python3.12 || command -v python3)"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or newer is required"'
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0755 "$APP_DIR/.venv"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/.venv"
runuser -u "$APP_USER" -- "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --upgrade pip
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
runuser -u "$APP_USER" -- env ERROR_ARCHIVER_PROFILE_STORE="$STATE_DIR/profiles.json" "$APP_DIR/.venv/bin/python" -c 'from pathlib import Path; import os; from modules.profile_store import ensure_profile_store; ensure_profile_store(Path(os.environ["ERROR_ARCHIVER_PROFILE_STORE"]))'
chown "$APP_USER:$APP_GROUP" "$STATE_DIR/profiles.json"
chmod 0600 "$STATE_DIR/profiles.json"
if [[ -n "$CONTROL_PROFILE" ]]; then
  control_args=(--profile "$CONTROL_PROFILE" --schema "$CONTROL_SCHEMA" --user "$CONTROL_USER" --secret-ocid "$CONTROL_SECRET")
  if [[ "${ERROR_ARCHIVER_IMPORT_LEGACY:-0}" == 1 ]]; then
    control_args+=(--import-directory "$STATE_DIR")
  fi
  runuser -u "$APP_USER" -- env ERROR_ARCHIVER_PROFILE_STORE="$STATE_DIR/profiles.json" "$APP_DIR/.venv/bin/python" "$APP_DIR/configure_control.py" "${control_args[@]}"
fi
# Keep the checked-out Git worktree root-owned for safe `sudo git pull` reruns.
# The service needs write access only to .venv and /var/lib/error-log-archiver.
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec chown -R root:root {} +
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/.venv"
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
MySQL Log Archiver installed. Workers stay disabled until the control profile is configured; fresh job settings default to disabled.
Open HTTPS on port 443 in both the host firewall and OCI NSG/security list.
NOTICE
if [[ -f "$STATE_DIR/settings.json" ]]; then
  echo 'Existing JSON data is retained. Use Control DB to import it into an empty schema before resuming jobs.'
fi
echo 'Sign in to configure the archive control schema and test its worker credential Secret OCID.'
echo 'Another Compute can install the exported profiles.json as /var/lib/error-log-archiver/profiles.json (service-owned, mode 0600).'
echo "Use: systemctl status error-log-archiver-web error-log-archiver.timer"
