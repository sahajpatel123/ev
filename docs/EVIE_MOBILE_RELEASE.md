# EVIE Mobile — Release System

**Ship path:** Safari PWA at `https://<home>.ts.net/evie/` over Tailscale. Funnel off. **No Xcode. No IPA. No TestFlight.**

Upgrades are the same origin: Home Station serves a new PWA build (`release.json` / hello `latest_web_build`); the phone reloads.

```
change on Home Station
  → make pwa-release-manifest (if web assets changed)
  → phones open /evie/ (or already-installed Home Screen app)
  → hello sees latest_web_build → tap update / SW reload
```

Install on a phone:

1. Tailscale connected.
2. Safari → `https://<home>.ts.net/evie-install/` or `/evie/`.
3. Share → Add to Home Screen.
4. Pair with a Mac-issued code; promote on the Mac.

IPA / Ad Hoc / GitHub Actions signing remains in-tree as an **optional later** native track. It is not required to use Evie on iPhone.

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

## Product path and evidence

The shippable iPhone product is the **Tailscale `/evie/` PWA**. See [`docs/IPHONE_PRODUCT.md`](IPHONE_PRODUCT.md). EvieShell IPA is optional and not required.

Name the evidence class whenever you claim phone behavior:

- Automated: `make iphone-parity-check`
- Packaged: `EvieBrokerCheck` (Swift CLI, no Xcode.app)
- Physical two-iPhone: Safari / Home Screen on both phones via `scripts/ios/physical-acceptance.sh`

The phone always uses `https://<host>.ts.net`. Never `http://<tailscale-ip>:8000`.

## Physical two-iPhone gates

These require the same PWA origin on the iPhone 16 Pro and iPhone SE, Tailscale connected, Funnel off:

1. Add to Home Screen from Safari on both phones.
2. Pair and promote each independently; confirm `PAIRED_SANDBOX` → `TRUSTED_OWNER_DEVICE` and a bumped `auth_revision`.
3. Set camera role: 16 Pro preferred, SE fallback.
4. Ten spoken turns on each phone, including reconnect, interruption, and transfer.
5. Look, memory query, and a safe routed Mac action from both phones.
6. Capture offline, reconnect, exactly-once sync (queued is never executed).
7. Reload after a PWA build bump.
8. Revoke one phone; the other stays usable; the revoked phone loses access.
