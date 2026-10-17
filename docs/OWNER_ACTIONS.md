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
2. Review `EV_FABRIC_BULK_CONFIRM_N=25` — lower if you want earlier confirm gates on bulk ops.
3. Chat first-token latency: UNVERIFIED — no provider keys in this environment; run the echo double-baseline (`time` two identical chat prompts via API vs CLI) with keys configured and record TTFT here before claiming any improvement.
4. iPhone relay (S17): PARTIAL — turn-level test passes; OWNER-ACTION to pair a real device and confirm end-to-end relay receipt.
