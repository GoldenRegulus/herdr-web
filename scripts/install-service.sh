#!/bin/bash
# Install herdr-web as a launchd service.
#
# herdr-web listens on loopback only. The oauth2-proxy container in
# ~/docker/herdr-web is the single exposed surface: it publishes port 8765 on
# every interface of the Mac, including the Tailscale address, and reaches
# herdr-web through host.docker.internal.
#
# Running pieces live in ~/services/herdr-web: a local copy of the application,
# the virtual environment, and the logs. The source of truth stays in this
# repository; this script copies it, because a process that launchd starts
# cannot read code from the SMB share.
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
PORT="${HERDR_WEB_PORT:-8766}"

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
  for _ in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then return 0; fi
    sleep 2
  done
  echo "could not load $label" >&2
  return 1
}

remove_agent() {
  local label="$1"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  rm -f "$AGENTS/$label.plist"
}

label="com.regulus.herdr-web.loopback"
plist="$AGENTS/$label.plist"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/herdr-web</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
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
  <key>StandardOutPath</key><string>$LOGS/loopback.log</string>
  <key>StandardErrorPath</key><string>$LOGS/loopback.log</string>
</dict>
</plist>
PLIST

load_agent "$label" "$plist"
echo "installed $label on 127.0.0.1:$PORT"

# The proxy publishes 8765 on every interface, so herdr-web no longer binds the
# LAN address and nothing forwards the Tailscale port.
remove_agent com.regulus.herdr-web.lan
remove_agent com.regulus.herdr-web.forward

for _ in $(seq 1 100); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "healthy: herdr-web on 127.0.0.1:$PORT"
    break
  fi
  sleep 0.3
done
curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
  || { echo "FAILED: herdr-web did not start on $PORT" >&2; exit 1; }

for host in 127.0.0.1 192.168.2.1 100.70.11.77; do
  code=$(curl -sS -m 3 -o /dev/null -w '%{http_code}' "http://$host:8765/ping" || true)
  echo "proxy on $host:8765 -> ${code:-no answer}"
done

echo "logs: $LOGS"
