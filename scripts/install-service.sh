#!/bin/bash
# Install herdr-web as a launchd service.
#
# Running pieces live in ~/services/herdr-web: a local copy of the application,
# the virtual environment, and the logs. The source of truth stays in this
# repository; this script copies it. A process that launchd starts cannot read
# code from an SMB share, and the share is unavailable at login.
#
# Usage: scripts/install-service.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="$HOME/services/herdr-web"
APP="$SERVICE/app"
VENV="$SERVICE/venv"
LOGS="$SERVICE/logs"
PYTHON="${HERDR_WEB_PYTHON:-/opt/homebrew/bin/python3}"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

mkdir -p "$APP" "$LOGS" "$AGENTS"

# Copy the application to local disk. launchd cannot execute code that lives on
# the SMB share.
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO/herdr_web/" "$APP/herdr_web/"
cp "$REPO/pyproject.toml" "$REPO/README.md" "$APP/"

if [ ! -x "$VENV/bin/herdr-web" ]; then
  echo "creating $VENV"
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet "$APP"

install_agent() {
  local name="$1" host="$2"
  local label="com.regulus.herdr-web.$name"
  local plist="$AGENTS/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/herdr-web</string>
    <string>--host</string><string>$host</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOGS/$name.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$name.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$plist"
  echo "installed $label on $host:8765"
}

install_agent loopback 127.0.0.1
install_agent lan 192.168.2.1

for host in 127.0.0.1 192.168.2.1; do
  for _ in $(seq 1 100); do
    if curl -fsS -m 2 "http://$host:8765/healthz" >/dev/null 2>&1; then
      echo "healthy: $host:8765"
      break
    fi
    sleep 0.3
  done
  curl -fsS -m 2 "http://$host:8765/healthz" >/dev/null 2>&1 \
    || { echo "FAILED to start on $host" >&2; exit 1; }
done

echo "logs: $LOGS"
