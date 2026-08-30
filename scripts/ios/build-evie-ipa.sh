#!/usr/bin/env bash
# =============================================================================
# EVIE NATIVE — canonical iOS build: archive → Ad Hoc export → verify → package
#
# One command owns the whole pipeline (directive B9). Designed to run on a
# managed macOS CI runner with full Xcode (the Home Station intentionally has
# only Command Line Tools and must never become build infrastructure).
#
# Required environment:
#   EXPORT_TEAM_ID          Apple Developer Team ID            (e.g. ABCDE12345)
#   EXPORT_CERT_NAME        Distribution certificate identity  ("Apple Distribution: ...")
#   EV_API_URL              Home Station origin                (https://<host>.ts.net)
# Optional:
#   CHANNEL                 canary|stable   (default canary)
#   NATIVE_BUILD_NUMBER     CFBundleVersion (default: epoch-based, monotonic)
#   MARKETING_VERSION       CFBundleShortVersionString (default 1.0)
#   UDID_PRIMARY/UDID_SECONDARY  verified against the embedded profile (B14)
# Output:
#   build/ios-release/<channel>/  → Evie.ipa, release.json, SHA256SUMS
#
# Secrets are consumed from a temporary keychain created by CI; this script
# never prints or persists signing material.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="$REPO_ROOT/ios/EvieShell/EvieShell.xcodeproj"
SCHEME="Evie"
CONFIGURATION="Release"
CHANNEL="${CHANNEL:-canary}"
OUT_DIR="${BUILD_DIR:-$REPO_ROOT/build/ios-release}/$CHANNEL"

log() { printf '\n[evie-ios] %s\n' "$*"; }
fail() { printf '\n[evie-ios] FAIL at stage %s: %s\n' "$1" "$2" >&2; exit "20$((RANDOM % 10))"; }

for req in EXPORT_TEAM_ID EXPORT_CERT_NAME EV_API_URL; do
  [ -n "${!req:-}" ] || fail "SOURCE" "missing required env: $req"
done

NATIVE_BUILD_NUMBER="${NATIVE_BUILD_NUMBER:-$(date +%s)}"
MARKETING_VERSION="${MARKETING_VERSION:-1.0}"
ARCHIVE="$OUT_DIR/Evie.xcarchive"
EXPORT_DIR="$OUT_DIR/export"

rm -rf "$OUT_DIR"; mkdir -p "$OUT_DIR"

# ---- stage: TEST ------------------------------------------------------------
log "stage TEST: broker contract check (fast, deterministic)"
( cd "$REPO_ROOT/ios/EvieShell" && swift run -c release EvieBrokerCheck >/dev/null ) \
  || fail "TEST" "EvieBrokerCheck failed"

# ---- stage: SOURCE prep ------------------------------------------------------
# Ephemeral substitutions in the CI workspace only; never committed.
log "stage SOURCE: stamp version + API origin into Info.plist"
PLIST="$REPO_ROOT/ios/EvieShell/App/Info.plist"
plutil -replace CFBundleVersion        -string "$NATIVE_BUILD_NUMBER" "$PLIST"
plutil -replace CFBundleShortVersionString -string "$MARKETING_VERSION" "$PLIST"
plutil -replace EV_API_URL             -string "$EV_API_URL" "$PLIST"

# ---- stage: ARCHIVE ----------------------------------------------------------
log "stage ARCHIVE: xcodebuild archive ($SCHEME, $CONFIGURATION)"
xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration "$CONFIGURATION" \
  -destination 'generic/platform=iOS' \
  -archivePath "$ARCHIVE" \
  CURRENT_PROJECT_VERSION="$NATIVE_BUILD_NUMBER" \
  MARKETING_VERSION="$MARKETING_VERSION" \
  DEVELOPMENT_TEAM="$EXPORT_TEAM_ID" \
  CODE_SIGN_STYLE=Manual \
  archive \
  || fail "ARCHIVE" "xcodebuild archive failed"

# ---- stage: SIGNING/EXPORT ---------------------------------------------------
log "stage EXPORT: Ad Hoc export with registered devices"
cat > "$OUT_DIR/export-options.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>ad-hoc</string>
  <key>teamID</key><string>$EXPORT_TEAM_ID</string>
  <key>signingCertificate</key><string>$EXPORT_CERT_NAME</string>
  <key>stripSwiftSymbols</key><true/>
  <key>compileBitcode</key><false/>
</dict>
</plist>
EOF
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE" \
  -exportOptionsPlist "$OUT_DIR/export-options.plist" \
  -exportPath "$EXPORT_DIR" \
  || fail "SIGNING" "exportArchive failed (certificate/profile/devices)"

IPA="$(find "$EXPORT_DIR" -name '*.ipa' -maxdepth 1 -print -quit)"
[ -n "$IPA" ] || fail "EXPORT" "no IPA produced"
mv "$IPA" "$OUT_DIR/Evie.ipa"

# ---- stage: VERIFY -----------------------------------------------------------
log "stage VERIFY: signature, profile, devices, metadata"
VERIFY_ARGS=(--ipa "$OUT_DIR/Evie.ipa" --expect-bundle-id com.ev.evie.shell
             --expect-version "$MARKETING_VERSION" --expect-build "$NATIVE_BUILD_NUMBER")
[ -n "${UDID_PRIMARY:-}" ]   && VERIFY_ARGS+=(--expect-udid "$UDID_PRIMARY")
[ -n "${UDID_SECONDARY:-}" ] && VERIFY_ARGS+=(--expect-udid "$UDID_SECONDARY")
"$REPO_ROOT/scripts/ios/verify-release.sh" "${VERIFY_ARGS[@]}" \
  || fail "VERIFY" "artifact verification failed — DO NOT PUBLISH"

# ---- stage: PACKAGE ----------------------------------------------------------
log "stage PACKAGE: release manifest + checksums"
COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
XCODE_VER="$(xcodebuild -version | head -1 | awk '{print $2}')"
SDK_VER="$(xcodebuild -showsdk -sdk iphoneos 2>/dev/null | awk '/sdkVersion/{print $2}')"
SHA256="$(shasum -a 256 "$OUT_DIR/Evie.ipa" | awk '{print $1}')"

python3 - "$OUT_DIR" <<PYEOF
import json, sys, datetime
out = sys.argv[1]
release = {
    "channel": "$CHANNEL",
    "app_version": "$MARKETING_VERSION",
    "native_build": "$NATIVE_BUILD_NUMBER",
    "commit": "$COMMIT",
    "web_core_build": json.load(open("$REPO_ROOT/backend/clients/pwa/release.json"))["web_build"],
    "native_shell_version": "1.0.0",
    "broker_protocol": 1,
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "minimum_ios": "17.0",
    "ipa_sha256": "$SHA256",
    "ipa_file": "Evie.ipa",
    "xcode": "$XCODE_VER",
    "sdk": "$SDK_VER",
}
release["web_protocol_min"] = json.load(open("$REPO_ROOT/backend/clients/pwa/release.json"))["web_protocol"]
open(out + "/release.json", "w").write(json.dumps(release, indent=2) + "\n")
open(out + "/SHA256SUMS", "w").write(f"{release['ipa_sha256']}  Evie.ipa\n")
print(json.dumps({k: release[k] for k in ("channel","app_version","native_build","commit","ipa_sha256")}, indent=2))
PYEOF

log "DONE: $OUT_DIR/Evie.ipa (build $NATIVE_BUILD_NUMBER, channel $CHANNEL)"
