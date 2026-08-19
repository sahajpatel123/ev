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
LIFE_HELPER="$ROOT/.build/release/EVLifeHelper"
APP="$ROOT/build/EV.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN" "$APP/Contents/MacOS/EV"
cp "$HELPER" "$APP/Contents/MacOS/EVNotificationHelper"
cp "$LIFE_HELPER" "$APP/Contents/MacOS/EVLifeHelper"
cp "$ROOT/Resources/Info.plist" "$APP/Contents/Info.plist"
cp -R "$ROOT/Resources/orb" "$APP/Contents/Resources/orb"

if [[ ! -f "$APP/Contents/Resources/orb/filament-orb.mp4" ]]; then
    echo "ERROR: filament-orb.mp4 missing from $APP" >&2
    exit 1
fi
if [[ ! -f "$APP/Contents/Resources/orb/BUILD_ID.txt" ]]; then
    echo "ERROR: BUILD_ID.txt missing from $APP" >&2
    exit 1
fi
if grep -R -l 'three.module.js\|UnrealBloomPass\|index.html' "$APP/Contents/Resources/orb" >/dev/null 2>&1; then
    echo "ERROR: stale Three.js orb assets were copied into $APP" >&2
    exit 1
fi
if ! grep -a -q 'metal-video-quad-v4-presence' "$APP/Contents/MacOS/EV"; then
    echo "ERROR: packaged binary is missing the current orb renderer identity" >&2
    exit 1
fi
if grep -a -q 'VoiceOrbFallbackView' "$APP/Contents/MacOS/EV"; then
    echo "ERROR: packaged binary still contains the WKWebView orb fallback" >&2
    exit 1
fi
echo "Orb identity: $(tr '\n' ' ' < "$APP/Contents/Resources/orb/BUILD_ID.txt")"

# Sign with the stable self-signed "EV Code Signing" identity created by
# signing.sh. TCC ties every permission grant to the app's code signature;
# ad-hoc (--sign -) signatures change the CDHash on every rebuild, so each
# rebuild looks like a brand-new app and all granted permissions are silently
# forgotten. The fixed identity keeps the signature (and therefore the TCC
# grants) stable across rebuilds.
IDENTITY="$(./scripts/signing.sh 2>/dev/null || true)"
if [ -n "$IDENTITY" ]; then
    codesign --force --deep --sign "$IDENTITY" \
        --entitlements "$ROOT/Resources/EV.entitlements" \
        "$APP" >/dev/null 2>&1
    codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || {
        echo "ERROR: codesign --verify failed for $APP" >&2
        exit 1
    }
    echo "Signed $APP with stable identity: $IDENTITY"
    echo "Signature: $(codesign -dv --verbose=4 "$APP" 2>&1 | grep -E '^(Signature|TeamIdentifier|Identifier)')"
else
    echo "ERROR: no stable code-signing identity available." >&2
    echo "       TCC permission grants are tied to the code signature; an ad-hoc" >&2
    echo "       signature changes its CDHash on every rebuild, so macOS forgets" >&2
    echo "       every granted permission (Microphone, Camera, …) after each build." >&2
    echo "       Run ./scripts/signing.sh to create the 'EV Code Signing' identity," >&2
    echo "       then re-run ./scripts/package.sh." >&2
    exit 1
fi

echo "Packaged $APP"

# DO NOT CHANGE — sync_api_env writes EV_MASTER_KEY (>=16 chars) only.
# Never copy EV_EARS_API_KEY / "dev" / "changeme" here; that 401s EV.app.
# The GUI app is not launched from the repo shell, so copy the local API
# credentials where AppConfig can find them. Never echo the key.
sync_api_env() {
    local src="$ROOT/../.env"
    local dest="${HOME}/Library/Application Support/EV/api.env"
    mkdir -p "$(dirname "$dest")"
    local url="http://127.0.0.1:8000"
    local key=""
    if [[ -f "$src" ]]; then
        url="$(awk -F= '/^EV_API_URL=/{v=$2} END{print v}' "$src" | tr -d "\"'")"
        key="$(awk -F= '/^EV_MASTER_KEY=/{v=$2} END{print v}' "$src" | tr -d "\"'")"
        if [[ -z "$key" || ${#key} -lt 16 ]]; then
            key="$(awk -F= '/^EV_API_KEY=/{v=$2} END{print v}' "$src" | tr -d "\"'")"
        fi
    fi
    url="${EV_API_URL:-${url:-http://127.0.0.1:8000}}"
    key="${EV_MASTER_KEY:-${EV_API_KEY:-$key}}"
    if [[ -n "$key" && "$key" != "dev" && ${#key} -ge 16 ]]; then
        umask 077
        printf 'EV_API_URL=%s\nEV_API_KEY=%s\nEV_MASTER_KEY=%s\n' "$url" "$key" "$key" > "$dest"
        defaults write com.ev.suit EV_API_URL "$url"
        defaults write com.ev.suit EV_API_KEY "$key"
        echo "Wrote API credentials for EV.app → $dest"
    else
        echo "WARNING: no EV_API_KEY/EV_MASTER_KEY found; EV.app will 401 on chat/voice." >&2
    fi
}
sync_api_env
