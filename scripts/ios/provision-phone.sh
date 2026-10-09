#!/bin/bash
# Provision a second iPhone over Tailscale: mint a pairing token, build the
# pairing URL, and render it as a QR PNG. The new phone scans the QR with the
# stock Camera app, Safari opens the PWA with ?pair=, and the PWA redeems the
# token once, then strips it from the address bar. No typing, no master key
# on the phone.
#
# Usage: EV_MASTER_KEY=<master> scripts/ios/provision-phone.sh ["Phone SE"]
# Requires: ev.api on 127.0.0.1:8000, Tailscale logged in + Serve on :443.
set -u

DISPLAY_NAME="${1:-Evie phone}"
ROLE="${EV_PROVISION_ROLE:-primary_companion}"
API="http://127.0.0.1:8000"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${EV_MASTER_KEY:-}" ]; then
  echo "MISSING: EV_MASTER_KEY is required (it never leaves this Mac)." >&2
  exit 1
fi

TAILSCALE_BIN=""
for c in "/Applications/Tailscale.app/Contents/MacOS/Tailscale" "/usr/local/bin/tailscale" "/opt/homebrew/bin/tailscale" "$HOME/Applications/Tailscale.app/Contents/MacOS/Tailscale"; do
  if [ -x "$c" ]; then TAILSCALE_BIN="$c"; break; fi
done
if [ -z "$TAILSCALE_BIN" ] && command -v tailscale >/dev/null 2>&1; then TAILSCALE_BIN="$(command -v tailscale)"; fi
if [ -z "$TAILSCALE_BIN" ]; then
  echo "MISSING: tailscale binary not found. Install Tailscale and sign in." >&2
  exit 1
fi

DNS="$("$TAILSCALE_BIN" status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); s=(d.get("Self") or {}).get("DNSName",""); print(s.strip("."))' 2>/dev/null)"
if [ -z "${DNS:-}" ]; then
  echo "MISSING: Tailscale is not logged in (no MagicDNS). Run: $TAILSCALE_BIN up" >&2
  exit 1
fi
if ! curl -s -m 5 "$API/v1/device-gateway/health" >/dev/null 2>&1; then
  echo "MISSING: ev.api is not answering on :8000. Start it first (make dev)." >&2
  exit 1
fi

MINTED="$(curl -s -m 10 -X POST "$API/v1/device-gateway/pairing-tokens" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"role": sys.argv[1], "display_name": sys.argv[2]}))' "$ROLE" "$DISPLAY_NAME")")"
TOKEN="$(printf '%s' "$MINTED" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("pairing_token",""))' 2>/dev/null)"
EXPIRES="$(printf '%s' "$MINTED" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("expires_at",""))' 2>/dev/null)"
if [ -z "${TOKEN:-}" ]; then
  echo "FAILED: could not mint a pairing token. Server said:" >&2
  printf '%s\n' "$MINTED" | head -c 400 >&2
  exit 1
fi

PAIR_URL="$(python3 -c 'import sys,urllib.parse; print("https://" + sys.argv[1] + "/evie/?pair=" + urllib.parse.quote(sys.argv[2], safe=""))' "$DNS" "$TOKEN")"
OUT="/tmp/evie-pair-$(date +%Y%m%d-%H%M%S).png"
if ! swift "$SCRIPT_DIR/qr-render.swift" "$PAIR_URL" "$OUT" >/dev/null 2>&1; then
  echo "FAILED: QR render failed (swift + CoreImage required on this Mac)." >&2
  echo "Pairing URL (type or AirDrop it instead):" >&2
  printf '%s\n' "$PAIR_URL" >&2
  exit 1
fi

echo "Displaying QR for: $DISPLAY_NAME (role $ROLE)"
echo "Expires: ${EXPIRES:-unknown} — scan promptly; the PWA redeems it once."
echo "PNG: $OUT"
open "$OUT" 2>/dev/null || echo "Open this file to show the QR: $OUT"
