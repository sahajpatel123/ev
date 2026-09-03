#!/usr/bin/env bash
# Physical two-iPhone acceptance for the Tailscale Safari PWA at /evie/.
# No Xcode. No IPA required. IPA verify is optional if someone still has one.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

IPA="${IPA:-}"
REQUIRE_PHYSICAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ipa) IPA="$2"; shift 2 ;;
    --require-physical) REQUIRE_PHYSICAL=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done

fail() { echo "FAIL PHYSICAL: $*" >&2; exit 1; }

echo "[physical] automated PWA syntax"
node --check backend/clients/pwa/app.js
node --check backend/clients/pwa/webrtc.js

echo "[physical] swift broker check (no Xcode.app required)"
( cd ios/EvieShell && swift run EvieBrokerCheck )

echo "[physical] pytest gates"
(
  cd backend
  uv run pytest -q \
    tests/test_iphone_capability_plan.py \
    tests/test_g2_trust_lifecycle.py \
    tests/test_release_contract.py \
    tests/test_device_gateway.py \
    tests/test_pwa_audio.py \
    tests/test_webrtc_connection.py \
    tests/test_release_portal.py
)

if [ -n "$IPA" ]; then
  echo "[physical] optional IPA $IPA"
  ./scripts/ios/verify-release.sh --ipa "$IPA" --expect-bundle-id com.ev.evie.shell
else
  echo "[physical] IPA not part of this path — Safari PWA over Tailscale is the install"
fi

cat <<'EOF'

Physical two-iPhone remaining (Safari / Home Screen, not Xcode):
  [ ] Tailscale connected on both phones, Funnel disabled
  [ ] Open https://<host>.ts.net/evie/ (or /evie-install/) in Safari
  [ ] Share → Add to Home Screen on iPhone 16 Pro and iPhone SE
  [ ] Pair + Mac-promote each phone; trust_state and auth_revision visible
  [ ] Home Screen card or More → Settings: 16 Pro as preferred camera, SE as fallback
  [ ] Ten spoken turns / reconnect / interruption / lease transfer on each
  [ ] Look, memory query, Mac-safe action
  [ ] Offline capture + exactly-once replay (pending != executed)
  [ ] Reload after a PWA build bump (hello latest_web_build / tap update line)
  [ ] Revoke one phone; the other remains usable

Evidence class: automated here; physical only after the boxes above.
EOF

if [ "$REQUIRE_PHYSICAL" = "1" ]; then
  fail "physical two-iPhone evidence is not recorded; complete the checklist on both devices"
fi

echo "[physical] automated OK (Tailscale PWA path)"
