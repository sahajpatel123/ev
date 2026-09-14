# iPhone Program Closeout — C1-C50

Status: **50/50 cycles shipped to `main`**. Final build: see
`backend/app/device_gateway/__init__.py` (`PWA_BUILD`).

## What shipped

| Phase | Cycles | Delivered |
|---|---|---|
| Foundation | C1-C6 | trust tiers + capability manifest, phone tool dispatch, camera Look, HUD cards, timers with ring UI |
| Presence & Intelligence | C7-C14 | shadow memory injection, recall_history, Web Push, quiet-hour nudges, quick actions + Siri endpoint, battery awareness, offline exactly-once queue, streaming states |
| Voice Depth | C15-C24 | owner-confirmed barge-in, PTT mode, typed-answer TTS, voice identity parity, emotion prosody, morning brief, calendar + reminders voice, sensitive read tier (Mail/Messages), search_web |
| Advanced | C25-C40 | EV Sense consented-sensor panel, consented heading-out geofence, photo memory keep, enrolled-people recognition, voice enrollment, speaker-verified sends, live partial transcript, history + provenance chips, memory browser, tactical brief, SE compact + performance profiles, two-iPhone lease arbitration, cross-device handoff, push-to-wake, Wake Lock ambient |
| Hardening | C41-C50 | E2E canary, contract re-lock (453 v1 paths), TTFA metrics + dev overlay, payload hardening, privacy transparency, one-command release bump, reconnect/resume drill, full icon/splash identity, parity matrix, closeout |

## The laws (non-negotiable, machine-checked where possible)

1. **Display == enforcement.** The capability manifest shows exactly what
   endpoints enforce, per trust state. Checked: `make phone-parity-matrix`.
2. **Health numbers never reach a model.** `sent_to_model` is always
   False; checked in the parity matrix and EV Sense.
3. **Location is consent + evaluate-and-drop.** No history is stored;
   consent revocable in EV Sense.
4. **Voice is an owner surface.** Enrollment and speaker verification
   refuse sandbox devices (403). Raw enrollment clips are never stored;
   the voiceprint is encrypted.
5. **Sends are person-gated.** A paired token proves the device; the
   voice check proves the owner. send_message/place_call refuse without
   a fresh 120 s verification.
6. **One conversation.** Lease arbitration refuses a silent takeover;
   explicit `takeover` wins; the old holder learns via conversation_moved.
7. **No-innerHTML PWA.** All rendering is DOM-constructed; the gate
   `test_pwa_is_installable_and_has_no_provider_secrets` holds.
8. **Release coherence.** One build across all pin sites;
   `scripts/bump_build.sh` + `docs/RELEASE_FLOW.md`.

## Run the gates

```bash
make phone-voice-e2e        # E2E canary + capability suite
make phone-parity-matrix    # display == enforcement
make phone-reconnect-drill  # drop → resume → replay, nothing doubled
make release-bump           # pins + manifest together
make iphone-parity-check    # syntax + release contract
```

Full closeout set (all green at closeout): 139 passed, 1 skipped.

## Known limits (honest)

- Heading-out and wake are foreground-PWA constructs; iOS PWA has no
  background geofencing and none was faked.
- Speaker verification uses the configured verifier; on test runtimes it
  is the documented test double, not a security control.
- The trusted (non-sandbox) phone paths require a trust promotion from
  the Mac ("promote this iPhone") — sandbox devices see the honest
  locked-down surface by design.

## Where to work next

Post-50 backlog lives in the parent repo's `docs/NEXT_STEPS.md` and the
`evvision/` plan; the phone program's entry points are
`docs/RELEASE_FLOW.md`, the E2E canary, and the parity matrix.
