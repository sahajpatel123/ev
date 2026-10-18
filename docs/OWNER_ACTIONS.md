# Owner Actions — TCC Deep-Link Checklist

Owner-gated steps the app cannot do itself. Open each link, toggle, return.

- [ ] Microphone: `x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone`
- [ ] Camera: `x-apple.systempreferences:com.apple.preference.security?Privacy_Camera`
- [ ] Screen Recording: `x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture`
- [ ] Accessibility: `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`
- [ ] Automation / AppleEvents: `x-apple.systempreferences:com.apple.preference.security?Privacy_Automation`
- [ ] Full Disk Access: `x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles`
- [ ] Location Services: `x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices`
- [ ] Notifications: `x-apple.systempreferences:com.apple.preference.notifications`
- [ ] Bluetooth: `x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth`
- [ ] Local Network: `x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork`

Verify: `tccutil` must NOT be used to bypass; confirm toggles show Evie/EV entries ON.

## Fabric owner actions (2026-10-17, branch evie-fabric)
1. Set `EV_FABRIC_ROOTS` to your allowlisted workspace roots (comma-separated). Empty = all file/shell lanes deny.
   Verifier: `EV_FABRIC_ROOTS=$HOME/Code backend/.venv/bin/python -m pytest backend/tests/e2e_fabric -q` → 41 passed.
2. Review `EV_FABRIC_BULK_CONFIRM_N=25` — lower if you want earlier confirm gates on bulk ops.
3. Chat first-token latency: UNVERIFIED — no provider keys in this environment; run the echo double-baseline (`time` two identical chat prompts via API vs CLI) with keys configured and record TTFT here before claiming any improvement.
4. iPhone relay (S17): PARTIAL — turn-level test passes; OWNER-ACTION to pair a real device and confirm end-to-end relay receipt.
5. Real EventKit reminders (S13): the fabric test covers the lane contract only. To go live: grant Reminders to Evie.app (`x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders`), then run a JXA reminder create+readback and paste the receipt.
6. Tailscale remote check: install/locate the Tailscale CLI, verify Serve ON / Funnel OFF, then run the cellular reachability test from the iPhone (PWA at the Tailscale Serve URL). Record pass/fail here.
7. Ghostty remote control: NOT available in Ghostty 1.3.1 (probe in BUILD_LOG) — tmux is the terminal addressing layer. No action unless Ghostty ships scripting; then re-probe with `ghostty +help`.

## Final run (2026-10-18) — ranked by what blocks a release claim

1. **Pair a real iPhone** (WP11). On the Mac: `ev setup phone` prints a
   pairing code; on the phone open the Tailscale Serve URL `/evie` → Share →
   Add to Home Screen → pair with the code. Then confirm: approvals inbox
   lists a parked action, an attached image reaches the turn, Stop cancels.
   Nothing on that path is claimed as device-verified until this is done.
2. **Shortcuts**: in the PWA, Mobile Actions → download the signed bridge
   shortcut; verify "Ask Evie" from Shortcuts returns a reply over the
   tailnet origin.
3. **Gmail live send** (WP8): no OAuth credentials on this host. Draft/send
   code paths are exercised with doubles and the send is parked for approval;
   the real send is yours to authorize (the code never sends without a grant).
4. **WhatsApp** (WP8): the linked Chrome profile is absent
   (`~/.ev/chrome-cdp/.linked`), so sends are not live. Link the profile, then
   a draft→approve→send round trip can be recorded.
5. **iMessage/Mail sends** (WP8): wired and fake-tested; a real send was not
   performed because the brief forbids sending real messages. Verify yourself
   from the PWA approval card.
6. **Deploy the phone panel**: the tailnet origin proxies the *deployed* build,
   which predates `/evie/evie-phone.js`. `ev setup phone` reports that as SKIP
   until `scripts/deploy_production.sh` publishes this tree.
7. **Latency**: measured (docs/EVIE_LATENCY.md, 25 real trials). TTFT is not
   instrumented; streaming replies to speech is the next real lever.
8. **Voice on-device check**: `ev models status` is green (wake head, tiny ASR,
   Kokoro, mic). The wake-word round trip needs you to say "hey Evie" with
   `ev setup`'s voice phase running, because it listens to the mic.
9. **Code-by-voice delegation**: agent CLIs (opencode/claude/codex/gemini) are
   present and wired, but live delegation spends your quota — invoke it
   yourself once, then the job store/resume/fix loop is proven end to end.
