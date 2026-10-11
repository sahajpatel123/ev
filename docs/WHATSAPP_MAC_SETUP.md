# WhatsApp on the Mac — owner setup (Executor Plan Phase B)

Three transports, each with one setup ceremony. Do them once; Evie degrades
honestly when any piece is missing.

## Transport A — Desktop cache reads (needs Full Disk Access)

Evie reads `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`
read-only (`mode=ro` + `PRAGMA query_only=ON`). It never marks messages read
and never claims the cache is live.

1. Install WhatsApp Desktop from the Mac App Store and open it once so it syncs.
2. Grant Full Disk Access to the process that runs Evie:
   - System Settings → Privacy & Security → Full Disk Access → `+` →
     add Terminal (or the app that hosts `EVLifeHelper` / the backend venv).
3. Verify: `backend/.venv/bin/python scripts/whatsapp_connection.py` should
   show `"read_available": true` with `"read_source": "desktop_cache"`
   (or `"cdp"` when linked).

If the cache is unreadable Evie says exactly that ("open WhatsApp Desktop
once so it syncs") instead of inventing messages.

## Transport B — Headless browser sends/reads (needs one link ceremony)

Evie drives its own isolated Chrome profile (`~/.ev/chrome-cdp`,
overrides `EV_WHATSAPP_CDP_PROFILE` / `EV_WHATSAPP_CDP_PORT`). Your everyday
browser is never touched.

1. Start the workspace: `backend/.venv/bin/python scripts/whatsapp_connection.py --connect`
2. Save a private QR: `... --qr /private/tmp/ev-whatsapp-link.png`
   (mode 0600, never leaves the Mac; scan it promptly — codes expire).
3. On the iPhone: WhatsApp → Settings → Linked Devices → Link a Device → scan.
4. Re-run without flags: `"authenticated": true`.

WhatsApp caps linked devices (~4). If the Mac loses its slot you will hear
"lost its linked-device slot — re-link it"; re-run the ceremony. Evie never
evicts your other devices to take a slot.

The `ev.whatsapp_cdp` launchd job (every 5 min) keeps the workspace alive
across reboots and records state to `~/.ev/whatsapp-cdp-supervisor.json`.
Install with `./launchd/install.sh`.

## Transport C — Desktop AX sends (needs Accessibility)

Background sends through WhatsApp Desktop without activating it:

1. System Settings → Privacy & Security → Accessibility → add and enable
   `EVLifeHelper` (or run `EVLifeHelper whatsapp.ax_request_access` once —
   macOS opens the pane for you).
2. If a macOS update silently drops the grant: remove the entry, re-add it,
   retry the send.

## What Evie will and will not do

- Reads never mark messages read on any transport.
- Sends verify a new outgoing message before claiming success; an uncertain
  send is reported honestly and never auto-retried.
- Drafts and opened compose windows are never delivery evidence.
