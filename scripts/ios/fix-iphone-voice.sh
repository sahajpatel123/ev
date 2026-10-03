#!/bin/bash
# Fix iPhone voice (both phones) — Tailscale Serve HTTPS is the only voice-capable origin.
# Root cause: http://100.x:8000 and http://127.0.0.1:8000 are NOT secure contexts,
# so iOS WKWebView/Safari blocks getUserMedia (webrtc M01) and the mic button dies.
# Fix: `tailscale serve https / http://localhost:8000`, phones open https://<mac>.ts.net/evie/.
# Backend stays on localhost (secure); Serve terminates TLS. Never enable Funnel.
set -u
TAILSCALE_BIN=""
for c in "/Applications/Tailscale.app/Contents/MacOS/Tailscale" "/usr/local/bin/tailscale" "/opt/homebrew/bin/tailscale" "$HOME/Applications/Tailscale.app/Contents/MacOS/Tailscale"; do
  if [ -x "$c" ]; then TAILSCALE_BIN="$c"; break; fi
done
if [ -z "$TAILSCALE_BIN" ] && command -v tailscale >/dev/null 2>&1; then TAILSCALE_BIN="$(command -v tailscale)"; fi

echo "== 1/4 Tailscale =="
if [ -z "$TAILSCALE_BIN" ]; then
  echo "MISSING: install Tailscale (App Store or brew install --cask tailscale-app), sign in, rerun."
  exit 1
fi
echo "binary: $TAILSCALE_BIN"
"$TAILSCALE_BIN" status 2>&1 | head -n 15
DNS="$("$TAILSCALE_BIN" status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); s=(d.get("Self") or {}).get("DNSName",""); print(s.strip("."))' 2>/dev/null)"
echo "MagicDNS: ${DNS:-unknown}"
echo ""
echo "== 2/4 Serve (HTTPS, Funnel must stay OFF) =="
"$TAILSCALE_BIN" serve status 2>&1 | head -n 20
"$TAILSCALE_BIN" funnel status 2>&1 | head -n 10
if [ -n "${DNS:-}" ]; then
  echo ""
  echo "Phones must open: https://$DNS/evie/"
  echo "NEVER: http://<100.x>:8000 (mic blocked by iOS secure-context rule)"
fi
echo ""
echo "To enable (once): $TAILSCALE_BIN serve --bg --https=443 http://127.0.0.1:8000"
echo "  or: EV_TAILSCALE_SERVE_APPLY=1 make evie-cross-platform-ready"
echo ""
echo "== 3/4 Backend on this Mac =="
if lsof -i :8000 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
  lsof -i :8000 -sTCP:LISTEN 2>/dev/null | head -n 10
else
  echo "Not listening on :8000. Start: make dev  (keeps localhost bind; Serve proxies it)"
fi
curl -s -m 5 http://127.0.0.1:8000/v1/device-gateway/health 2>&1 | head -c 600; echo ""
echo ""
echo "== 4/4 Phone checklist (do on BOTH iPhones) =="
cat <<'EOF'
1. Tailscale app: connected, same tailnet, Funnel OFF.
2. Safari (not EvieShell http build): open https://<mac>.ts.net/evie/
   - Share -> Add to Home Screen (optional, same origin).
3. Self-test on phone: More -> Self-test. Require HTTPS=PASS, Microphone=PASS.
   - If HTTPS=FAIL you opened http://100.x — close it, reopen the https URL.
4. Pair each phone: get code on Mac (device-gateway pairing-token), pair in /evie/,
   then on Mac promote to owner: POST /v1/device-gateway/admin/promote-owner.
   Confirm TRUSTED_OWNER_DEVICE (not PAIRED_SANDBOX).
5. Tap Talk (mic). Grant Microphone when iOS prompts. Speak 10s, confirm transcript.
6. EvieShell IPA (optional): defaults write com.ev.evie.shell evie.api_origin -string "https://<mac>.ts.net"
   then relaunch. TrustedOrigin now allows 100.x for diagnostics, but voice still needs https.
EOF
