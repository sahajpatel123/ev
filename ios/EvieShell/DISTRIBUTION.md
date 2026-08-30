# Evie Mobile Shell — distribution

**Selected path (owner):** automated macOS CI → Ad Hoc sign → private Tailnet OTA.  
See `docs/EVIE_MOBILE_RELEASE.md` for the full release system, secrets checklist, and portal UX.

**Not used for routine development:** TestFlight · Xcode GUI Run · public IPA hosting.

Status honesty:

| Layer | Status |
|---|---|
| CI workflow / scripts / portal | IMPLEMENTED (needs Apple secrets + first archive) |
| Ad Hoc signing / OTA | IMPLEMENTED in pipeline · PHYSICAL VERIFICATION PENDING |
| Native shell app | SCAFFOLDED |
| Native voice A/B vs PWA golden | NOT STARTED (gate before Stable) |

## Phase 0 facts

- Home Station runs Evie Core; it is **not** the CI build host.
- GitHub Actions `macos-15` owns `xcodebuild` archive + Ad Hoc export.
- Deployment target: **iOS 17** (16 Pro + SE on iOS 17+).
- AlarmKit is iOS 26+; older OS uses Evie notification timer (spoken as Evie timer, never Clock).
- Bundle id: `com.ev.evie.shell`.

## Owner install (after first Canary publishes)

1. On Primary, open `https://<home-station>.ts.net/evie-install/` (Tailscale on).
2. Canary → Install.
3. Launch Evie · pair if asked · voice A/B vs PWA golden.
4. Only after Primary passes: Secondary.

Exceptional Xcode GUI use is for native debugging only — not the release loop.
