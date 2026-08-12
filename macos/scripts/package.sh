#!/bin/zsh
# Package the SwiftPM-built EV menu-bar app into build/EV.app without Xcode.
#
# Usage: ./scripts/package.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

swift build -c release

BIN="$ROOT/.build/release/EV"
HELPER="$ROOT/.build/release/EVNotificationHelper"
APP="$ROOT/build/EV.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN" "$APP/Contents/MacOS/EV"
cp "$HELPER" "$APP/Contents/MacOS/EVNotificationHelper"
cp "$ROOT/Resources/Info.plist" "$APP/Contents/Info.plist"

# Ad-hoc signing gives launchd/TCC a stable identity for the local app. A
# Developer ID signature can be substituted later for distribution.
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "Packaged $APP"
