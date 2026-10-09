#!/usr/bin/env bash
# =============================================================================
# Install EvieShell on a USB-connected iPhone with the Home Station origin.
#
# Owner action (not CI): connect the iPhone by cable, unlock it, tap Trust,
# then run this script. It stamps EV_API_URL (auto-detected from Tailscale
# unless provided), builds the app with automatic signing, installs it, and
# launches it. First launch is already configured — no `defaults write`.
#
# Usage:
#   scripts/ios/install-evie-iphone.sh
#   EV_API_URL=https://my-mac.ts.net scripts/ios/install-evie-iphone.sh
#   DEVELOPMENT_TEAM=ABCDE12345 scripts/ios/install-evie-iphone.sh
#   DEVICE_UDID=<udid> scripts/ios/install-evie-iphone.sh
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BUNDLE_ID="com.ev.evie.shell"
TS_BIN="${TAILSCALE_BIN:-/Applications/Tailscale.app/Contents/MacOS/Tailscale}"

log() { printf '\n[evie-install] %s\n' "$*"; }
fail() { printf '\n[evie-install] FAIL: %s\n' "$*" >&2; exit 2; }

command -v xcodebuild >/dev/null 2>&1 || fail "xcodebuild not found. Install Xcode and select it."
command -v xcrun >/dev/null 2>&1 || fail "xcrun not found."

# ---- origin -----------------------------------------------------------------
EV_API_URL="${EV_API_URL:-}"
if [ -z "$EV_API_URL" ] && [ -x "$TS_BIN" ]; then
  DNS="$("$TS_BIN" status --json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
print(((data.get("Self") or {}).get("DNSName") or "").rstrip("."))
' || true)"
  if [ -n "$DNS" ]; then
    EV_API_URL="https://$DNS"
  fi
fi
if [ -z "$EV_API_URL" ]; then
  fail "could not determine the Home Station origin. Rerun with EV_API_URL=https://<mac>.<tailnet>.ts.net"
fi
log "Home Station origin: $EV_API_URL"

# ---- device -----------------------------------------------------------------
DEVICE_UDID="${DEVICE_UDID:-}"
if [ -z "$DEVICE_UDID" ]; then
  xcrun devicectl list devices --json-output /tmp/evie-devicectl.json >/dev/null 2>&1 || true
  DEVICE_UDID="$(python3 - <<'PY'
import json
try:
    data = json.load(open('/tmp/evie-devicectl.json'))
except Exception:
    raise SystemExit(0)
for dev in data.get('result', {}).get('devices', []):
    props = dev.get('properties') or {}
    legacy = dev.get('deviceProperties') or {}
    visibility = str(props.get('visibilityClass') or '')
    reality = str(props.get('reality') or legacy.get('reality') or '')
    tunnel = str((dev.get('connectionProperties') or {}).get('tunnelState') or '')
    name = str(props.get('name') or legacy.get('name') or dev.get('name') or '')
    if visibility == 'simulators' or reality in ('simulated', 'simulator'):
        continue
    if reality == 'physical' or ('iPhone' in name and tunnel in ('connected', 'available')):
        print(dev.get('identifier', ''))
        break
PY
)"
fi
if [ -z "$DEVICE_UDID" ]; then
  fail "no connected iPhone found. Connect the iPhone by cable, unlock it, tap Trust, then rerun (or pass DEVICE_UDID=...)."
fi
log "Device: $DEVICE_UDID"

# ---- build ------------------------------------------------------------------
DERIVED="$ROOT/build/ios-device"
TEAM_ARGS=()
if [ -n "${DEVELOPMENT_TEAM:-}" ]; then
  TEAM_ARGS=(DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM")
fi

log "Building EvieShell for the device (automatic signing)…"
xcodebuild \
  -project ios/EvieShell/EvieShell.xcodeproj \
  -scheme Evie \
  -configuration Debug \
  -destination "id=$DEVICE_UDID" \
  -derivedDataPath "$DERIVED" \
  -allowProvisioningUpdates \
  "${TEAM_ARGS[@]}" \
  EV_API_URL="$EV_API_URL" \
  build

APP="$DERIVED/Build/Products/Debug-iphoneos/Evie.app"
[ -d "$APP" ] || fail "build product not found at $APP"

STAMPED="$(/usr/libexec/PlistBuddy -c 'Print EV_API_URL' "$APP/Info.plist" 2>/dev/null || true)"
if [ "$STAMPED" != "$EV_API_URL" ]; then
  fail "Info.plist origin is '$STAMPED', expected '$EV_API_URL' (build-setting substitution failed)."
fi
log "Stamped origin verified in the built app."

# ---- install + launch -------------------------------------------------------
log "Installing on the iPhone…"
xcrun devicectl device install app --device "$DEVICE_UDID" "$APP"

log "Launching Evie…"
xcrun devicectl device process launch --device "$DEVICE_UDID" "$BUNDLE_ID" || true

log "Done. First launch asks nothing: the Home Station address is already set."
log "If it shows the Connect screen instead, the device build ignored EV_API_URL — enter $EV_API_URL once there."
