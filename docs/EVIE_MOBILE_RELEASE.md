# EVIE Mobile — Release System

**Status:** pipeline IMPLEMENTED (local verification) · signing/OTA PENDING owner Apple setup + physical install.

Architecture (owner-selected): automated macOS CI → Ad Hoc signing → private tailnet OTA. **No TestFlight. No routine Xcode GUI.**

```
commit → GitHub Actions macOS runner
  → TEST      swift run EvieBrokerCheck
  → ARCHIVE   xcodebuild archive (Release, generic/platform=iOS)
  → SIGN      Ad Hoc export, manual signing, pinned team/cert/profile
  → VERIFY    scripts/ios/verify-release.sh (signature, bundle, version,
              build, profile type, expiry, BOTH device UDIDs) — fail = no publish
  → PACKAGE   release.json + SHA256SUMS
  → PUBLISH   POST /evie-install/admin/publish (master-key; checksum re-verified)
  → INSTALL   https://<home>.ts.net/evie-install/  (tailnet-only, funnel OFF)
```

## Channels

| Channel | Meaning | Who fills it |
| --- | --- | --- |
| `canary` | Latest CI build approved for physical testing | CI (`make ios-canary` / workflow dispatch) |
| `stable` | Last OWNER-VERIFIED build | Explicit promotion only (`make ios-stable-promote`) |

A new commit NEVER auto-replaces Stable. Promotion copies the exact tested artifact (no rebuild). Previous stable is archived (`releases/archive/stable/<build>`, last 3 kept) for rollback.

## Portal (on the phones)

Open `https://<home-station>.ts.net/evie-install/` in Safari on a registered iPhone:

```
EVIE MOBILE
Stable · Version X · Build Y    [install]
Canary · Version X · Build Z    [install]
```

Channel links serve an `itms-services` OTA manifest pointing at the IPA over the same trusted HTTPS. iOS requires the Tailscale app connected during install.

Admin endpoints (master key only): `POST /evie-install/admin/publish?channel=…` (multipart: `release_json`, `ipa`; server re-verifies SHA-256), `POST /evie-install/admin/promote?from_build=N`.

## Versioning (independent axes)

| Axis | Source |
| --- | --- |
| Web Core build | `backend/clients/pwa/release.json` (generated; see test_release_contract.py) |
| Native CFBundleVersion | monotonic epoch build stamped by CI |
| App marketing version | `MARKETING_VERSION` input |
| Native shell / broker protocol | `EvieNativeBroker.Contract` (1.0.0 / 1) |

## Provisioning lifecycle (B35–B37)

* `verify-release.sh` fails the pipeline if the embedded profile expires within 14 days and always prints days remaining.
* Renewal: regenerate Ad Hoc profile in the Apple account (same devices) → update `ADHOC_PROFILE_BASE64` secret → next canary carries it. No code changes.
* New phone: register UDID → regenerate profile → update secret → next build covers it.

## Security model (B38–B40)

* Signing material lives ONLY in GitHub Actions secrets; imported into an ephemeral keychain per job, deleted after.
* Workflow runs only on `workflow_dispatch` from `main`. PRs cannot reach signing.
* IPAs are served exclusively through the tailnet (funnel off, localhost bind). Nothing public.
* Upload endpoint refuses any manifest/IPA checksum mismatch.

---

## ONE-TIME OWNER SETUP CHECKLIST (irreducible)

Everything below requires the Apple account holder. There is no way to automate these.

1. ☐ **Apple Developer Program membership active** (individual is fine).
2. ☐ **Team ID** — developer.apple.com → Membership details (10-char, e.g. `ABC123DEFG`).
3. ☐ **Register both iPhones** — developer.apple.com → Devices:
     - Primary iPhone 16 Pro UDID
     - Secondary iPhone SE UDID
     (UDIDs: Settings → General → About, or Finder when plugged in once.)
4. ☐ **Create Ad Hoc provisioning profile** — Identifiers → new App ID `com.ev.evie.shell` (explicit) → Profiles → new **Ad Hoc** profile for that App ID with BOTH devices → download the `.mobileprovision`.
5. ☐ **Create Apple Distribution certificate** — if none exists: Certificates → + → Apple Distribution → download `.cer`, export `.p12` with password from Keychain Access on a Mac that has it.
6. ☐ **Put secrets into GitHub repo** (Settings → Secrets and variables → Actions):
     - `APPLE_TEAM_ID`
     - `DISTRIBUTION_CERT_P12` (base64: `base64 -i cert.p12 | pbcopy`)
     - `DISTRIBUTION_CERT_PASSWORD`
     - `ADHOC_PROFILE_BASE64` (base64 of the mobileprovision)
     - `EVIE_INSTALL_TOKEN` (= Home Station master key)
     - `EV_API_URL` (= `https://<home-station>.ts.net`)
     - `UDID_PRIMARY`, `UDID_SECONDARY`

Never paste any of these into chat.

## Routine loop after setup

1. Orchestrator lands a change.
2. GitHub → Actions → `evie-ios` → Run workflow (canary).
3. Owner opens the portal on the Primary → Canary → Install.
4. Voice A/B vs PWA golden (B27). Pass → orchestrator runs `make ios-stable-promote FROM_BUILD=<build>`.
5. Secondary installs Stable (or Canary if enrolled).

## Status vocabulary (honest)

| Item | Status |
| --- | ---|
| CI workflow + build/verify/package scripts | IMPLEMENTED (syntax-checked, unit-tested around portal; full run needs secrets) |
| Ad Hoc signing | IMPLEMENTED in pipeline / VERIFIED pending first real build |
| OTA install | IMPLEMENTED / PHYSICAL VERIFICATION PENDING |
| Native shell app | SCAFFOLDED (compiles as SwiftPM logic; iOS app target needs first archive) |
| Native voice A/B | NOT STARTED (gate before any promotion) |
