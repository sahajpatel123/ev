#!/bin/zsh
# PULSE launchd installer: api, worker, scheduler, runtime, ears, collector.
#
# Default: installs as LaunchAgents (starts at login, no sudo needed).
#   ./launchd/install.sh
#
# Boot-time (LaunchDaemons, needs sudo, starts before login):
#   sudo ./launchd/install.sh --system
#
# Uninstall: ./launchd/uninstall.sh [--system]

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="agent"
if [[ "${1:-}" == "--system" ]]; then
  MODE="system"
fi

if [[ "$MODE" == "system" ]]; then
  TARGET_DIR="/Library/LaunchDaemons"
  DOMAIN="system"
else
  TARGET_DIR="$HOME/Library/LaunchAgents"
  DOMAIN="gui/$UID"
fi

LOG_DIR="$HOME/Library/Logs/ev"
mkdir -p "$LOG_DIR"

# Build the native notification helper once (no sudo needed).
HELPER_DIR="$ROOT/backend/storage/notify"
mkdir -p "$HELPER_DIR"
HELPER_BIN="$HELPER_DIR/EVNotificationHelper.app/Contents/MacOS/EVNotificationHelper"
if [[ ! -x "$HELPER_BIN" ]]; then
  echo "[ev] building macOS notification helper"
  (cd "$ROOT/backend" && swiftc -O -framework Foundation -framework UserNotifications \
    -o "$HELPER_BIN" \
    app/notify/macos/EVNotificationHelper.swift)
  cat > "$HELPER_DIR/EVNotificationHelper.app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>ev.pulse</string>
    <key>CFBundleName</key><string>EVNotificationHelper</string>
    <key>CFBundleDisplayName</key><string>EV</string>
    <key>CFBundleExecutable</key><string>EVNotificationHelper</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
PLIST
  codesign --force --deep -s - "$HELPER_DIR/EVNotificationHelper.app"
fi

PLISTS=(api worker scheduler runtime ears collector opencode)
for name in "${PLISTS[@]}"; do
  plist="$ROOT/launchd/ev.$name.plist"
  plutil -lint "$plist" >/dev/null
  label="ev.$name"
  if [[ "$MODE" == "system" ]]; then
    sudo launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    sudo cp "$plist" "$TARGET_DIR/"
    sudo launchctl bootstrap "$DOMAIN" "$TARGET_DIR/ev.$name.plist"
    sudo launchctl enable "$DOMAIN/$label"
    # ears is a companion to the EV menu-bar app: it must not run by itself.
    # The app starts it on launch and stops it on quit, so the microphone is
    # only ever active while the menu-bar app is open.
    if [[ "$name" != "ears" ]]; then
      sudo launchctl kickstart -k "$DOMAIN/$label"
    fi
  else
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    cp "$plist" "$TARGET_DIR/"
    launchctl bootstrap "$DOMAIN" "$TARGET_DIR/ev.$name.plist"
    launchctl enable "$DOMAIN/$label"
    if [[ "$name" != "ears" ]]; then
      launchctl kickstart -k "$DOMAIN/$label"
    fi
  fi
  echo "[ev] $label loaded ($MODE)"
done

echo "[ev] launchd install complete. Logs: $LOG_DIR"
echo "[ev] log rotation: sudo cp launchd/ev.newsyslog.conf /etc/newsyslog.d/ev.conf"
