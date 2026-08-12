#!/bin/zsh
# PULSE launchd uninstaller. Opposite of install.sh.

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

for name in api worker scheduler runtime ears collector; do
  label="ev.$name"
  plist="$TARGET_DIR/ev.$name.plist"
  if [[ -f "$plist" ]]; then
    if [[ "$MODE" == "system" ]]; then
      sudo launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
      sudo rm -f "$plist"
    else
      launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
      rm -f "$plist"
    fi
    echo "[ev] $label removed ($MODE)"
  fi
done
