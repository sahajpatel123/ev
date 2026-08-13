#!/bin/zsh
# Package the SwiftPM-built EV menu-bar app into build/EV.app without Xcode.
#
# TCC (the privacy database) records permissions against a code signature, so
# the signing steps below are not cosmetic: a bundle that fails to sign never
# appears in System Settings > Privacy & Security and can never hold a grant.
# Every codesign invocation therefore fails the build instead of being ignored.
#
# Usage:
#   ./scripts/package.sh              # build + sign into build/EV.app
#   ./scripts/package.sh --install    # ... then install into /Applications
#
# Environment:
#   EV_CODESIGN_IDENTITY  signing identity (default "-", ad-hoc). An ad-hoc
#                         identity is a cdhash, which changes on every rebuild,
#                         so macOS treats each build as a different app and
#                         previously granted permissions stop applying. Pass a
#                         Developer ID or a self-signed identity from your
#                         keychain (see `security find-identity -v -p
#                         codesigning`) for an identity that survives rebuilds.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

IDENTITY="${EV_CODESIGN_IDENTITY:--}"
ENTITLEMENTS="$ROOT/Resources/EV.entitlements"

swift build -c release

BIN="$ROOT/.build/release/EV"
HELPER="$ROOT/.build/release/EVNotificationHelper"
APP="$ROOT/build/EV.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN" "$APP/Contents/MacOS/EV"
cp "$HELPER" "$APP/Contents/MacOS/EVNotificationHelper"
cp "$ROOT/Resources/Info.plist" "$APP/Contents/Info.plist"

# A copied or downloaded bundle carries com.apple.quarantine, which makes macOS
# run the app from a random read-only App Translocation mount. Its TCC identity
# then changes on every launch and no grant ever sticks. `xattr -d` also exits
# non-zero when the attribute is already absent, which is the state we want.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

if [ "$IDENTITY" = "-" ]; then
    echo "codesign identity: - (ad-hoc)"
    echo "  warning: ad-hoc identities change on every rebuild. macOS will ask"
    echo "  for permissions again after each package run, and stale EV entries"
    echo "  can pile up in System Settings. Set EV_CODESIGN_IDENTITY to a real"
    echo "  identity for grants that survive rebuilds."
    # The hardened runtime is signable ad-hoc but buys nothing locally, and a
    # secure timestamp needs a real certificate.
    SIGN_OPTIONS=(--timestamp=none)
else
    echo "codesign identity: $IDENTITY"
    # The hardened runtime is only meaningful with a real identity; it also
    # requires the entitlements below or the mic/camera calls are refused.
    SIGN_OPTIONS=(--options runtime --timestamp)
fi

# The helper is a second Mach-O inside Contents/MacOS and must be signed before
# the outer bundle, otherwise the app signature seals an unsigned nested binary
# and verification fails. This is what --deep used to paper over; --deep is
# deprecated for signing and must not be used.
codesign --force --sign "$IDENTITY" \
    --identifier "com.ev.suit.notification-helper" \
    "${SIGN_OPTIONS[@]}" \
    "$APP/Contents/MacOS/EVNotificationHelper"

codesign --force --sign "$IDENTITY" \
    --identifier "com.ev.suit" \
    "${SIGN_OPTIONS[@]}" \
    --entitlements "$ENTITLEMENTS" \
    "$APP"

echo
echo "signature:"
codesign -dv --verbose=2 "$APP" 2>&1 | sed 's/^/  /'

echo
echo "verify:"
codesign --verify --strict --verbose=2 "$APP" 2>&1 | sed 's/^/  /'

echo
echo "spctl (report only; ad-hoc and self-signed builds are expected to be rejected):"
spctl --assess --type execute --verbose=4 "$APP" 2>&1 | sed 's/^/  /' || true

echo
echo "Packaged $APP"

if [ "$INSTALL" -eq 1 ]; then
    echo
    exec "$ROOT/scripts/install.sh"
fi

echo "Run ./scripts/install.sh to install into /Applications — TCC grants are"
echo "unstable while the app runs from build/."
