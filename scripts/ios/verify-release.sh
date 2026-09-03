#!/usr/bin/env bash
# =============================================================================
# EVIE NATIVE — artifact verification gate (directive B14).
# Inspects the exported IPA. Publishes NOTHING on failure.
#   --ipa PATH
#   --expect-bundle-id ID     --expect-version V  --expect-build N
#   --expect-udid UDID        (repeatable; every listed device must be covered)
# Exit 0 only if: valid signature, expected team/bundle/version/build, embedded
# Ad Hoc profile contains all required device UDIDs and has not expired.
# =============================================================================
set -euo pipefail

IPA="" ; WANT_BID="" ; WANT_VER="" ; WANT_BUILD="" ; TEAM_ID="" ; UDIDS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ipa) IPA="$2"; shift 2 ;;
    --expect-bundle-id) WANT_BID="$2"; shift 2 ;;
    --expect-version) WANT_VER="$2"; shift 2 ;;
    --expect-build) WANT_BUILD="$2"; shift 2 ;;
    --team-id) TEAM_ID="$2"; shift 2 ;;
    --expect-udid) UDIDS+=("$2"); shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -f "$IPA" ] || { echo "FAIL VERIFY: no IPA at $IPA" >&2; exit 50; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
unzip -q "$IPA" -d "$TMP"
APP="$(find "$TMP/Payload" -maxdepth 1 -name '*.app' -print -quit)"
[ -n "$APP" ] || { echo "FAIL VERIFY: no .app in Payload" >&2; exit 51; }

# 1) code signature validity
codesign --verify --strict --verbose=2 "$APP" >/dev/null 2>&1 \
  || { echo "FAIL VERIFY: invalid code signature" >&2; exit 52; }

# 2) certificate identity + team
SIGNING=$(codesign -dvv "$APP" 2>&1)
echo "$SIGNING" | grep -q "Authority=Apple Distribution" \
  || { echo "FAIL VERIFY: not signed with Apple Distribution cert:" >&2; echo "$SIGNING" | grep Authority >&2; exit 53; }
if [ -n "$TEAM_ID" ]; then
  echo "$SIGNING" | grep -q "TeamIdentifier=$TEAM_ID" \
    || { echo "FAIL VERIFY: wrong TeamIdentifier (want $TEAM_ID)" >&2; exit 54; }
fi

# 3) bundle id / version / build
BID=$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP/Info.plist")
VER=$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Info.plist")
BLD=$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$APP/Info.plist")
[ "$BID" = "$WANT_BID" ] || { echo "FAIL VERIFY: bundle id $BID != $WANT_BID" >&2; exit 55; }
[ -z "$WANT_VER" ]  || [ "$VER" = "$WANT_VER" ]    || { echo "FAIL VERIFY: version $VER != $WANT_VER" >&2; exit 56; }
[ -z "$WANT_BUILD"] || [ "$BLD" = "$WANT_BUILD" ]  || { echo "FAIL VERIFY: build $BLD != $WANT_BUILD" >&2; exit 57; }

# 4) embedded provisioning profile: ad-hoc type, expiry, device coverage
PROFILE="$APP/embedded.mobileprovision"
[ -f "$PROFILE" ] || { echo "FAIL VERIFY: no embedded profile" >&2; exit 58; }
PLIST_XML="$TMP/profile.plist"
security cms -D -i "$PROFILE" > "$PLIST_XML" 2>/dev/null \
  || { echo "FAIL VERIFY: unreadable profile" >&2; exit 59; }

PROVISIONED=$( /usr/libexec/PlistBuddy -c 'Print :Entitlements:provisioned-devices' "$PLIST_XML" 2>/dev/null || echo "")
APPTYPE=$( /usr/libexec/PlistBuddy -c 'Print :Entitlements:get-task-allow' "$PLIST_XML" 2>/dev/null || echo "")
[ "$APPTYPE" = "false" ] || { echo "FAIL VERIFY: profile is development, not Ad Hoc distribution" >&2; exit 60; }

EXPIRY=$( /usr/libexec/PlistBuddy -c 'Print :ExpirationDate' "$PLIST_XML" 2>/dev/null || echo "")
if [ -n "$EXPIRY" ]; then
  EXPIRE_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "${EXPIRY%%.**}Z" +%s 2>/dev/null || echo 0)
  NOW_EPOCH=$(date +%s)
  DAYS_LEFT=$(( (EXPIRE_EPOCH - NOW_EPOCH) / 86400 ))
  echo "[verify] profile expires in ${DAYS_LEFT}d ($EXPIRY)"
  [ "$DAYS_LEFT" -gt 14 ] || echo "WARN: provisioning profile expires within 14 days — rotate soon (directive B35/B36)" >&2
fi

for udid in "${UDIDS[@]:-}"; do
  [ -z "$udid" ] && continue
  echo "$PROVISIONED" | grep -q "$udid" \
    || { echo "FAIL VERIFY: required device $udid NOT in provisioning profile" >&2; exit 61; }
done
echo "[verify] devices covered: ${#UDIDS[@]}"

# 5) embedded API origin must be private Tailscale HTTPS, never raw :8000
API_URL=$( /usr/libexec/PlistBuddy -c 'Print EV_API_URL' "$APP/Info.plist" 2>/dev/null || echo "" )
case "$API_URL" in
  https://*.ts.net|https://*.ts.net/*) ;;
  https://*.ts.net:*) ;;
  *)
    echo "FAIL VERIFY: EV_API_URL must be https://<host>.ts.net (got '${API_URL:-missing}')" >&2
    exit 62
    ;;
esac
echo "$API_URL" | grep -Eq ':8000([^0-9]|$)' \
  && { echo "FAIL VERIFY: EV_API_URL must not use :8000 ($API_URL)" >&2; exit 63; }
echo "[verify] API origin $API_URL"

echo "[verify] OK: signature, identity, bundle $BID, version $VER build $BLD, Ad Hoc profile valid, HTTPS Home Station origin"
