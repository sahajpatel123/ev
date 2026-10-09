#!/bin/bash
# Evie voice doctor (READ-ONLY): diagnose a stuck "Thinking" turn on the
# Home Station Mac. Prints provider resolution, Tailscale/Serve state,
# endpoint health, and the telltale live-voice log lines. Never prints
# secret values, never restarts anything, never touches the database.
#
# Usage: scripts/evie-voice-doctor.sh   (run ON the Home Station Mac)
#   Optional: EV_MASTER_KEY=... for the authenticated diagnostics probe.
set -u

API="http://127.0.0.1:8000"
OUT_LOG="$HOME/Library/Logs/ev/api.out.log"
ERR_LOG="$HOME/Library/Logs/ev/api.err.log"

say() { printf '%s\n' "$1"; }
have() { [ -n "${!1:-}" ] && echo "set" || echo "empty"; }

say "=== 1. Voice brain resolution (values never printed) ==="
say "EV_VOICE_LIVE_BRAIN=${EV_VOICE_LIVE_BRAIN:-auto (default)}"
say "EV_GOOGLE_API_KEY=$(have EV_GOOGLE_API_KEY)"
say "EV_CHAT_PROVIDER=${EV_CHAT_PROVIDER:-echo (default)}"
say "EV_VOICE_ASR_PROVIDER=${EV_VOICE_ASR_PROVIDER:-auto (default)}"
say "EV_VOICE_TTS_PROVIDER=${EV_VOICE_TTS_PROVIDER:-auto (default)}"
BRAIN="${EV_VOICE_LIVE_BRAIN:-auto}"
if [ "$BRAIN" = "pipeline" ]; then EFFECTIVE="pipeline (ASR + chat + TTS)";
elif [ "$BRAIN" = "gemini" ]; then EFFECTIVE="gemini (Gemini Live S2S)";
elif [ -n "${EV_GOOGLE_API_KEY:-}" ]; then EFFECTIVE="gemini (auto + key set)";
else EFFECTIVE="pipeline (auto, no key)"; fi
say "EFFECTIVE_BRAIN=$EFFECTIVE"
say ""

say "=== 2. Tailscale + Serve ==="
TAILSCALE_BIN=""
for c in "/Applications/Tailscale.app/Contents/MacOS/Tailscale" "/usr/local/bin/tailscale" "/opt/homebrew/bin/tailscale"; do
  if [ -x "$c" ]; then TAILSCALE_BIN="$c"; break; fi
done
if [ -z "$TAILSCALE_BIN" ] && command -v tailscale >/dev/null 2>&1; then TAILSCALE_BIN="$(command -v tailscale)"; fi
if [ -z "$TAILSCALE_BIN" ]; then
  say "tailscale: NOT FOUND"
else
  DNS="$("$TAILSCALE_BIN" status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("Self") or {}).get("DNSName","").strip("."))' 2>/dev/null)"
  say "magic_dns=${DNS:-NOT LOGGED IN}"
  say "--- serve status ---"
  "$TAILSCALE_BIN" serve status 2>&1 | head -12
  say "--- funnel state ---"
  if "$TAILSCALE_BIN" funnel status 2>&1 | grep -qiE "off|disabled|not.*(on|enabled)|funnel.*false"; then
    say "funnel: off (correct)"
  else
    "$TAILSCALE_BIN" funnel status 2>&1 | head -4
  fi
fi
say ""

say "=== 3. Endpoint health (localhost) ==="
if curl -s -m 5 "$API/v1/device-gateway/health" 2>&1 | head -c 300; then echo ""; else say "health: UNREACHABLE"; fi
if [ -n "${EV_MASTER_KEY:-}" ]; then
  say "--- hands-free diagnostics (authenticated) ---"
  curl -s -m 10 "$API/v1/voice/hands-free/diagnostics" -H "Authorization: Bearer $EV_MASTER_KEY" 2>&1 | head -c 1200; echo ""
else
  say "(skip authenticated diagnostics: EV_MASTER_KEY not set)"
fi
say ""

say "=== 4. Telltale log lines (last 20 matches) ==="
for f in "$OUT_LOG" "$ERR_LOG"; do
  if [ -f "$f" ]; then
    say "--- $f ---"
    grep -aE "live_turn suppressed|live_respond|live send timed|realtime_|no_responder|turn_suppressed|voice_pipeline|asr_unavailable|gemini|Gemini|LIVE.*(error|fail|timeout|reconnect|pump)" "$f" 2>/dev/null | tail -20
  else
    say "--- $f MISSING ---"
  fi
done
say ""
say "=== 5. What to paste back ==="
say "Sections 1-4 above, plus: did your spoken words appear as text on the"
say "phone before it stuck? (yes/no). That plus EFFECTIVE_BRAIN pinpoints it."
