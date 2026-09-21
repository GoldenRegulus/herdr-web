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

# Replace a running agent. launchctl returns an input/output error when a
# bootstrap follows a bootout of the same label too closely, so wait for the
# label to disappear and retry.
load_agent() {
  local label="$1" plist="$2"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  for _ in $(seq 1 40); do
    launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
    sleep 0.25
  done
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then return 0; fi
    sleep 2
  done
  echo "could not load $label" >&2
  return 1
}

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
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERDR_BINARY</key><string>$(command -v herdr || echo /opt/homebrew/bin/herdr)</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOGS/$name.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$name.log</string>
</dict>
</plist>
PLIST
  load_agent "$label" "$plist"
  echo "installed $label on $host:8765"
}

install_agent loopback 127.0.0.1
install_agent lan 192.168.2.1

# The oauth2-proxy container in ~/docker/herdr-web listens on 127.0.0.1:4181.
# Docker cannot publish a container port on the Tailscale address, so forward
# the tailnet port to the proxy from the host.
FORWARD_PLIST="$AGENTS/com.regulus.herdr-web.forward.plist"
cat > "$FORWARD_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.regulus.herdr-web.forward</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/socat</string>
    <string>-d</string><string>-d</string>
    <string>TCP4-LISTEN:8765,bind=100.70.11.77,reuseaddr,fork</string>
    <string>TCP4:127.0.0.1:4181</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOGS/forward.log</string>
  <key>StandardErrorPath</key><string>$LOGS/forward.log</string>
</dict>
</plist>
PLIST
load_agent com.regulus.herdr-web.forward "$FORWARD_PLIST"
echo "installed com.regulus.herdr-web.forward on 100.70.11.77:8765" 

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
