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
# There is exactly ONE app: Evie.app. Earlier installs also dropped an
# /Applications/EV.app symlink so the short name would still resolve, but Finder
# draws a symlink to a bundle as a second app icon — which is where the "2-3
# Evie apps" confusion came from. Nothing needs it any more: presence.py and
# doctor.sh resolve /Applications/Evie.app and only fall back to the legacy name
# if it happens to exist. A stale symlink is removed (and unregistered) on install.
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
DEST="$DEST_DIR/Evie.app"
# Legacy short-name symlink from earlier installs. Removed, never recreated.
LEGACY_LINK="$DEST_DIR/EV.app"

# Quit any running copy by pid: replacing a bundle underneath a live process
# leaves the old signature running and TCC attributing grants to it.
# The pattern ends at a space or end-of-line so it does not also match
# Contents/MacOS/EVNotificationHelper. Match both Spotlight names.
if PIDS="$(pgrep -f '(Evie|EV)\.app/Contents/MacOS/EV( |$)' 2>/dev/null)"; then
    osascript -e 'quit app "EV"' >/dev/null 2>&1 || true
    osascript -e 'quit app "Evie"' >/dev/null 2>&1 || true
    sleep 1
    for pid in ${(f)PIDS}; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "stopping running EV (pid $pid)"
            kill "$pid" 2>/dev/null || true
        fi
    done
fi

# One app only. Unregister the legacy symlink *before* removing it: Launch
# Services keys that path, and a dangling entry is what keeps a ghost "EV" icon
# alive in Finder and Launchpad.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -e "$LEGACY_LINK" ] || [ -L "$LEGACY_LINK" ]; then
    if [ -x "$LSREGISTER" ]; then
        "$LSREGISTER" -u "$LEGACY_LINK" >/dev/null 2>&1 || true
    fi
    rm -rf "$LEGACY_LINK"
    echo "Removed legacy $LEGACY_LINK — one app only now: $DEST"
fi

rm -rf "$DEST"
cp -R "$APP" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

# Spotlight and Launchpad only list bundles Launch Services knows about.
if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -f -R "$DEST" >/dev/null 2>&1 || true
fi
mdimport "$DEST" >/dev/null 2>&1 || true
osascript >/dev/null 2>&1 <<EOF || true
tell application "Finder"
    set comment of (POSIX file "$DEST" as alias) to "Evie EV.app Talk menu bar"
end tell
EOF

# Drop the staging copy now that it is safely installed. build/EV.app is a
# second com.ev.suit bundle carrying the same CFBundleDisplayName "Evie", so
# leaving it behind makes Spotlight (Cmd-Space) list TWO identically named
# "Evie" apps and the owner cannot tell which one to open. `.metadata_never_index`
# does NOT prevent this on macOS 26 (verified: marker written before the bundle,
# bundle indexed anyway), so removal is the reliable fix. Re-run package.sh to
# rebuild the staging bundle.
if [[ -d "$APP" ]]; then
    if [[ -x "$LSREGISTER" ]]; then
        "$LSREGISTER" -u "$APP" >/dev/null 2>&1 || true
    fi
    rm -rf "$APP"
    echo "Removed build staging $APP (no longer indexed as a duplicate 'Evie')"
fi

echo "Installed $DEST"
echo "Spotlight: type Evie"
codesign --verify --strict --verbose=2 "$DEST" 2>&1 | sed 's/^/  /'

echo
echo "Next: open \"$DEST\", then use the menu-bar item > Permissions… >"
echo "\"Grant permissions\" and answer every dialog. That is what adds EV to"
echo "each Privacy pane (Microphone, Speech Recognition, Camera, Screen"
echo "Recording, Automation, Contacts, Calendars, Reminders, Notifications,"
echo "Bluetooth, Input Monitoring). Opening those panes first shows an empty"
echo "list. Full Disk Access has no prompt: Reveal EV.app and add it with +."
echo "Run ./scripts/doctor.sh if anything looks wrong."
