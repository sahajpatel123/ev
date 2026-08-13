#!/bin/zsh
# Install build/EV.app into /Applications (or ~/Applications) and clear the
# quarantine attribute.
#
# Why this matters: macOS records permissions against the app's code signature
# *and* the path it runs from. A bundle left in build/ still carries
# com.apple.quarantine, so Launch Services runs it through App Translocation —
# a random read-only mount under /private/var/folders — and its identity
# changes on every launch. Grants made in System Settings then apply to a copy
# that no longer exists. Installing into /Applications without quarantine is
# what makes a grant stick.
#
# Usage: ./scripts/install.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/EV.app"

if [ ! -d "$APP" ]; then
    echo "no build/EV.app — run ./scripts/package.sh first" >&2
    exit 1
fi

DEST_DIR="/Applications"
if [ ! -w "$DEST_DIR" ]; then
    DEST_DIR="$HOME/Applications"
    mkdir -p "$DEST_DIR"
    echo "/Applications is not writable; installing into $DEST_DIR"
fi
DEST="$DEST_DIR/EV.app"

# Quit any running copy by pid: replacing a bundle underneath a live process
# leaves the old signature running and TCC attributing grants to it.
# The pattern ends at a space or end-of-line so it does not also match
# Contents/MacOS/EVNotificationHelper.
if PIDS="$(pgrep -f 'EV\.app/Contents/MacOS/EV( |$)' 2>/dev/null)"; then
    osascript -e 'quit app "EV"' >/dev/null 2>&1 || true
    sleep 1
    for pid in ${(f)PIDS}; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "stopping running EV (pid $pid)"
            kill "$pid" 2>/dev/null || true
        fi
    done
fi

rm -rf "$DEST"
cp -R "$APP" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

echo "Installed $DEST"
codesign --verify --strict --verbose=2 "$DEST" 2>&1 | sed 's/^/  /'

echo
echo "Next: open \"$DEST\", then use the menu-bar item > Permissions… >"
echo "\"Grant permissions\". That request is what adds EV to System Settings >"
echo "Privacy & Security. Run ./scripts/doctor.sh if anything looks wrong."
