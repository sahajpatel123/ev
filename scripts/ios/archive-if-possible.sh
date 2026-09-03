#!/usr/bin/env bash
# Attempt an Evie IPA archive when Xcode is present. This Home Station
# often has only Command Line Tools — fail honest, never fake a signed IPA.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "[archive] SKIP: xcodebuild is not on PATH (Command Line Tools only). Packaged IPA remains an owner/CI gate."
  exit 0
fi
if ! xcodebuild -showsdks >/dev/null 2>&1; then
  echo "[archive] SKIP: xcodebuild is present but Xcode.app is not selected (active dir is Command Line Tools). Packaged IPA remains an owner/CI gate."
  exit 0
fi

echo "[archive] xcodebuild is present — running EvieBrokerCheck, then refusing unsigned local archive without EXPORT_TEAM_ID."
( cd ios/EvieShell && swift run EvieBrokerCheck )

if [ -z "${EXPORT_TEAM_ID:-}" ] || [ -z "${EXPORT_CERT_NAME:-}" ] || [ -z "${EV_API_URL:-}" ]; then
  echo "[archive] SKIP signed export: set EXPORT_TEAM_ID, EXPORT_CERT_NAME, and EV_API_URL then run scripts/ios/build-evie-ipa.sh"
  xcodebuild -project ios/EvieShell/EvieShell.xcodeproj -scheme Evie -showBuildSettings >/dev/null
  echo "[archive] scheme Evie is visible to xcodebuild"
  exit 0
fi

exec "$ROOT/scripts/ios/build-evie-ipa.sh"
