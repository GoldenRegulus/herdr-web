#!/bin/bash
# Install herdr-web as a launchd service that runs from local disk.
#
# The repository lives on an SMB share. A service that runs from a network
# share stops when the share is unavailable and cannot restart at boot. This
# script copies the application to a local directory, builds a local virtual
# environment, and installs one launchd agent per host.
#
# Usage: scripts/install-service.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# A path without spaces keeps generated shebang lines simple.
SUPPORT="$HOME/.local/share/herdr-web"
APP="$SUPPORT/app"
VENV="$SUPPORT/venv"
LOGS="$HOME/Library/Logs/herdr-web"
PYTHON="${HERDR_WEB_PYTHON:-/opt/homebrew/bin/python3}"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

mkdir -p "$APP" "$LOGS" "$AGENTS"

# Copy the application code to local disk.
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO/herdr_web/" "$APP/herdr_web/"
cp "$REPO/pyproject.toml" "$REPO/README.md" "$APP/"

# Build the local virtual environment when it is missing or stale.
if [ ! -x "$VENV/bin/herdr-web" ]; then
  echo "creating $VENV"
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade "$APP"

# Install one agent per host.
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

# Wait until every host answers before reporting success.
for host in 127.0.0.1 192.168.2.1; do
  for _ in $(seq 1 100); do
    if curl -fsS -m 2 "http://$host:8765/healthz" >/dev/null 2>&1; then
      echo "healthy: $host:8765"
      break
    fi
    sleep 0.3
  done
  curl -fsS -m 2 "http://$host:8765/healthz" >/dev/null 2>&1 || {
    echo "FAILED to start on $host" >&2
    tail -5 "$LOGS/${host/127.0.0.1/loopback}.log" >&2 || true
    exit 1
  }
done

echo "logs: $LOGS"
