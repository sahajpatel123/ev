# Build plan — all 49 MCU-derived **buildable** features in EV

**Status:** plan only. Do **not** implement in this document’s goal. Every item below names a later **real-time working target** a user can trigger (voice, HUD, API, or client). “Document it” is not the target.

**Source of truth for the 49:** `docs/MCU_AI_ASSISTANTS.md` §1 Buildable. Item numbers **1–49** match that file exactly.

**Out of this plan (futuristic — do not schedule):** Instant Kill / lethal modes; telecom backdoors / reading strangers’ texts; city-scale cameras or facial hunt; satellite / combat-drone weapons; House Party armor swarm; becoming Vision; telepathy math; organic-web DNA scans; omni-hack; Baby Monitor used to ID strangers. See `docs/MCU_AI_ASSISTANTS.md` §2.

**Honesty rule:** several items need hardware or vendor APIs this machine may lack (HealthKit, Apple Maps, OctoPrint, hobby drone, paid tickets, Face ID, home locks). They stay in the plan with a **later wave + offline fallback**. They are not dropped.

---

## Key decisions

1. **One voice loop is the product.** Almost every item is a tool, HUD card, or policy hooked into the existing wake → verify → listen → process → respond → follow-up path (`docs/VOICE.md`). Shipping 49 separate screens fails the “working in real time” bar.
2. **Reuse before invent.** EV already has personality, quiet hours, HUD schemas, health radar, gear scan, diagnostics/calibrate, research, maker queue, lookouts, device registry, Google Calendar read, EVLifeHelper (calls/mail/messages), WebAuthn, people whereabouts, EV Sense. Waves 1–2 are “make these live on the voice/HUD path,” not greenfield.
3. **Permissioned, owner-only actuators.** Calls, locks, printers, drones, cameras, location of *other people* require explicit owner consent and only touch **the owner’s** devices, accounts, and opted-in teammates.
4. **Adapters stay behind `IntegrationRegistry`.** New providers (Home Assistant, OctoPrint, Maps, ticket vendors, public records) are adapters with `local` doubles for CI and a real `provider` mode for the owner. Weather is **not** a new adapter: `get_weather` + Open-Meteo already ship.
5. **Training Wheels is a feature-flag gate, never a lethal gate.** Item 19 unlocks *this product’s* modes after onboarding — not Instant Kill.
6. **Futuristic items stay out.** This plan never schedules weapons, wiretaps, or city surveillance.

---

## What already exists (substrate)

| Surface | Where | What it already does |
|---|---|---|
| Voice lifecycle | `app/voice/lifecycle.py`, `/v1/voice/*` | Wake “EVIE”, owner verify, ASR/TTS, sleep phrases, follow-up window |
| Personality | `app/ev/personality.py` | Versioned tone sliders; `identity_block` for the model |
| Quiet hours / attention | `app/notify/policy.py`, `app/ev/ev_sense.py` | Quiet hours, daily cap, dedup; EV Sense intervention scoring |
| HUD | `app/ev/hud.py`, `docs/schemas/ev-hud-*.json` | `ev.hud.card.v1`, briefing, lookout, route, ops, focus |
| Lookout / presence | `app/ev/lookout.py`, web lookout/presence | Multi-panel visor on Mac/web |
| Diagnostics | `POST /v1/diagnostics/calibrate` | DB, embeddings, gateway, retrieval, storage |
| Health radar | `POST /v1/health/snapshot`, `/health/summary` | Readiness, anomalies, morning brief (API ingest; HealthKit later) |
| Gear | `POST /v1/gear/scan` | Device battery/storage/status; quiet-hours aware |
| Research | `app/ev/research.py` | Sessions, notes, citations, memory-grounded + optional web |
| Maker | `app/ev/maker.py` | Projects, BOM, print-job **queue** (no OctoPrint yet) |
| Navigation | `app/ev/navigation.py` | Next-event leave-by; **30 min stub**, no Maps |
| Calendar | Google Calendar adapter | Read-only sync + signals |
| Life actions | `EVLifeHelper` + `life_helper.py` | contacts, messages, mail, `call.place` (tel/FaceTime) with evidence |
| Device fleet | `docs/WAVE_LIFE.md` | Mac + phones registry, heartbeats, APNs, job routing |
| People | `GET /v1/people/{name}/whereabouts` | Last-seen in **owner memory**, not GPS of strangers |
| Identity | `app/identity/webauthn.py` | WebAuthn fail-closed |
| Alerts | watchlist + digest | Owner-subscribed topics; not a city spy grid |
| Smart home adapter | `adapters.py` slug `smart_home` | Local double + generic HTTP; not a live HomeKit path yet |
| Search | `app/search/` | Live web search providers |
| Weather | `get_weather` in `app/ev/tools.py`, `app/search/live.py` | First-class Open-Meteo current + 3-day forecast (no API key); tool + live search path |
| Vision | `app/ev/vision.py` | Attachment analysis / OCR (best-effort; not a deepfake verdict) |

---

## Build waves (order)

```text
Wave 0  Substrate contracts (session, tools, HUD, fleet)     — no user-facing “new feature”
Wave 1  Voice + personality live  (items 1–10)
Wave 2  Close existing APIs to voice/HUD  (12, 19–20, 22, 26–28, 34–38, 41, 48)
Wave 3  Life I/O  (13–15, 18, 21, 49)
Wave 4  Place, people, media  (16–17, 39, 42–44, 47)
Wave 5  Hardware + vendor later-wave  (23–25 complete, 30, 32–33, 40, 45–46)
```

**Dependencies (summary)**

- 1 is the trunk. 3–10 hang off 1.
- 11 and 31 require the device registry (exists) + one session identity.
- 21 depends on 13 (lights) and/or 45 (drone) and 11 (which device hears the command).
- 16 depends on 18’s calendar (read already exists) + a routing provider.
- 17 depends on 16’s location fix.
- 30 depends on 20 (print modes) and 22 (filament/empty).
- 33 and 45 share a telemetry ingest; 46 is a beacon sibling.
- 39 depends on 16’s opted-in share, not on people-memory whereabouts alone.
- 48 consumes 15, 35, 13, 41 as signal sources.
- 9 gates **all** proactive items (10, 22, 35, 36, 41, 48).

**Later-wave hardware (do not omit):** HealthKit (35), Apple/Google Maps (16–17), OctoPrint (30), hobby drone SDK (45, 33), paid tickets (18), Face ID / device biometrics (25), HomeKit/Home Assistant (13), owner cameras (40), AirTag/Find My or equivalent (46). Each has a CI `local` double so the voice/HUD path can be tested without the vendor.

---

## Coverage of all 49

For each item: **What** (same capability as `MCU_AI_ASSISTANTS.md`), **From**, **Exists vs gap**, **Real-time working target**, **Wave**.

### Voice, personality, companionship (1–10)

#### 1. A named voice companion you talk to all day
- **From:** All four (J.A.R.V.I.S., Karen, E.D.I.T.H., E.V.)
- **Exists:** Wake word, owner verify, ASR/TTS, chat, follow-up window, sleep phrases (`docs/VOICE.md`).
- **Gap:** Session still feels like “call an API,” not a day-long companion. No persistent spoken name the user chose. Mac ears + iOS + web are not one conversation identity.
- **Real-time target:** Owner says “EVIE” on Mac or phone, talks without re-waking through the follow-up window, hears TTS back, can say “that’s all” and it stops. Same thread continues on another paired device (item 11/31). CLI `ev ask` and web chat share the same conversation id.
- **Wave:** 1 (trunk). **Depends on:** voice lifecycle (exists).

#### 2. Accept a nickname (“Suit Lady” → “Karen”)
- **From:** Karen
- **Exists:** Personality profile has sliders, not a spoken display name the model must use. `Identity` / `display_name` exists on people, not on the assistant-as-heard.
- **Gap:** No owner-set **assistant nickname** stored and injected into every TTS/system prompt. No voice command “call yourself X.”
- **Real-time target:** Owner: “Call yourself Evie.” Next utterance the assistant answers as that name. Setting visible on web/Mac; persists across devices. Revert: “Go back to EVIE.”
- **Wave:** 1. **Depends on:** 1.

#### 3. Dry, loyal personality; “I may be malfunctioning”
- **From:** J.A.R.V.I.S.
- **Exists:** `personality.py` sliders + `identity_block`; UX.md specifies dry/honest tone.
- **Gap:** Sliders are not enforced on every live reply. No spoken **self-status** line when a subsystem is degraded (gateway down, mic failed, calibrate red).
- **Real-time target:** Personality update is audible on the next turn. If `/v1/diagnostics/calibrate` last run is `degraded`/`failed`, the assistant says so unprompted once per session (“I may be malfunctioning: chat gateway is down”) then continues.
- **Wave:** 1. **Depends on:** 1, 26.

#### 4. Introduce yourself and explain what you can do
- **From:** E.D.I.T.H.
- **Exists:** No first-run spoken capability tour. Docs exist; the running assistant does not list live protocols.
- **Gap:** Onboarding is not a real-time dialogue bound to **enabled** features (Training Wheels, item 19).
- **Real-time target:** First wake after install, or “What can you do?”, speaks a short honest list of **currently unlocked** capabilities (from item 5), offers to run Training Wheels. HUD card `ev.hud.card.v1` mirrors the list.
- **Wave:** 1. **Depends on:** 5, 19.

#### 5. “Not built only for you — you have these protocols” (honest capability list)
- **From:** E.D.I.T.H.
- **Exists:** Tools/actions registry and integration scopes exist internally.
- **Gap:** No owner-facing **live protocol sheet** (what is on, what needs a vendor, what is refused as futuristic).
- **Real-time target:** “What protocols do I have?” returns a spoken + HUD list: enabled / needs-setup / refused. Refused includes Instant Kill, wiretaps, city cameras — named so the owner hears the boundary.
- **Wave:** 1. **Depends on:** feature-flag table (19).

#### 6. Greet the current user by name; welcome them back
- **From:** E.D.I.T.H.
- **Exists:** Speaker verification knows *owner vs not*. People/identity store has names.
- **Gap:** No “welcome back” on session start using the owner’s preferred name. No distinct greeting when a **different enrolled owner-device user** is not allowed (single-owner invariant).
- **Real-time target:** On wake+verify, one spoken line: “Welcome back, {name}.” If verify fails, no greeting and no chat (`403 voice_ignored` stays). Text chat uses the same name.
- **Wave:** 1. **Depends on:** 1, speaker enrollment (exists).

#### 7. Play a short trust / dedication message from the person who set it up
- **From:** E.D.I.T.H. (“For the next Tony Stark, I trust you.”)
- **Exists:** Nothing like a founder note.
- **Gap:** Need a stored audio or text dedication, owner-editable, played once on first unlock and on demand (“Play the trust message”).
- **Real-time target:** Owner records or types a ≤30s note. First successful Training Wheels completion plays it. Later: “Play Tony’s— play the dedication.” TTS or recorded clip through the same speaker path as replies.
- **Wave:** 1. **Depends on:** 1, 19.

#### 8. Love / social advice, vault-night small talk
- **From:** Karen, E.V.
- **Exists:** Chat can already small-talk via the LLM. Companionship scan (`companionship.py`) detects isolation and points at human connection.
- **Gap:** Small talk is not a **mode** with a time budget; isolation guardrails are API-only, not spoken. Risk of becoming a fake girlfriend — must stay honest and push toward real people (`docs/EVIE_RESEARCH.md`).
- **Real-time target:** Owner can chat socially. If isolation scan trips, EV **says so once** and suggests a real person from memory — not “I am your only friend.” Quiet hours still apply (9).
- **Wave:** 1. **Depends on:** 1, 9, companionship scan (exists).

#### 9. Stay out of the way unless needed (quiet hours / attention budget)
- **From:** E.V. (less intrusive than Karen)
- **Exists:** `quiet_hours_active()`, notify policy (dedup, daily cap, emergency pierce), EV Sense `apply_attention_policy`.
- **Gap:** Voice follow-up and lookout auto-open can still feel “Karen.” Quiet hours must also mute **spoken** proactive lines (10, 22, 48), not just push.
- **Real-time target:** During configured quiet hours, EV does not speak or push unless emergency. Owner: “Go quiet until 8.” Immediate effect. Digest waiting items at quiet-hours end. Tests already exist for notify policy; extend to voice proactive.
- **Wave:** 1 (policy) + 2 (all proactive sources honor it). **Depends on:** settings (exists).

#### 10. Status callouts and “what just happened” narration
- **From:** E.V., Karen
- **Exists:** Lookout compose, notification titles, gear/health summaries — not a unified event→speech bus.
- **Gap:** No “callout channel”: calibrate finished, print job done, calendar in 15 min, gear battery low.
- **Real-time target:** When a watched event fires, EV speaks one sentence **if** attention policy allows, and posts `ev.hud.card.v1`. Owner: “What just happened?” replays the last N callouts.
- **Wave:** 2. **Depends on:** 1, 9, HUD (exists).

---

### House, lab, devices (11–25)

#### 11. One voice for home, workshop, and phone
- **From:** J.A.R.V.I.S.
- **Exists:** Device registry (Mac, Phone A/B), heartbeats, APNs, routing (`WAVE_LIFE.md`). Voice on ears/mac and iOS separately.
- **Gap:** Conversation and “who is speaking” are not one session across surfaces. Workshop (web workbench) is a third mouth.
- **Real-time target:** Start on Mac, continue on phone without re-explaining. TTS plays on the **attention-routed** device. Web workbench shows the same transcript live.
- **Wave:** 0–1. **Depends on:** registry (exists), 1.

#### 12. Import preferences onto a new device (“we’re online”)
- **From:** J.A.R.V.I.S. (“I have indeed been uploaded, sir.”)
- **Exists:** Pairing issues a device token; personality and memories live on the server (already shared).
- **Gap:** No pairing ceremony line, no push of **client prefs** (quiet hours display, HUD layout, nickname, Training Wheels progress) and no spoken “we’re online.”
- **Real-time target:** Pair a new phone → it pulls nickname, quiet hours, HUD layout, unlocked features. EV on that device says “We’re online.” Owner hears it once.
- **Wave:** 2. **Depends on:** 2, 9, 19, 11.

#### 13. Lights, locks, security, “is the garage closed”
- **From:** J.A.R.V.I.S.
- **Exists:** `smart_home` adapter with local double + HTTP passthrough. Not wired to a live Home Assistant / HomeKit home.
- **Gap:** No owner home inventory, no voice “turn off the bench lights,” no status query with evidence.
- **Real-time target:** Adapter `provider=homeassistant` (or HomeKit via a helper). Owner: “Are the lab lights on?” / “Lock the front door.” Success only with provider evidence (same honesty as `call.place`). **Fallback:** local double in CI; web toggles that the daemon treats as the house until a real hub is configured.
- **Wave:** 3 (live hub later-wave). **Depends on:** 11, 21, vault (exists).

#### 14. Place calls / “try this person”
- **From:** J.A.R.V.I.S., Karen
- **Exists:** `EVLifeHelper call.place` + `phone.call` / `facetime.call` routing; contacts.resolve. iOS CallKit manager exists in the tree.
- **Gap:** Not a first-class voice tool with confirm + evidence spoken back. “Try Miss Potts” needs people-memory + contacts.resolve.
- **Real-time target:** “Call Ned.” EV resolves via contacts (owner address book only), confirms, places tel/FaceTime, speaks “Ringing Ned” only if `data.opened == true`. Failure is spoken honestly.
- **Wave:** 3. **Depends on:** 1, people/contacts, LifeHelper (exists).

#### 15. Timekeeping, reminders, “37 minutes have passed”
- **From:** Karen
- **Exists:** Calendar signals, routines/scheduler, watchlist deadlines. No arbitrary spoken timer (“start a 37-minute clock”).
- **Gap:** Live timers and one-shot reminders that **speak** when they fire (gated by 9).
- **Real-time target:** “Remind me in 37 minutes.” / “How long have I been in this session?” Timer survives process restart (daemon). Fires as callout (10) + HUD.
- **Wave:** 2–3. **Depends on:** 9, 10, runtime daemon (exists).

#### 16. Fastest route, leave-by, “friends are at X” if they share location
- **From:** Karen, J.A.R.V.I.S.
- **Exists:** `route_briefing` uses next calendar event + **hardcoded 30 min**. People whereabouts = memory, not live GPS.
- **Gap:** No Maps ETA. No opted-in friend location share.
- **Real-time target:** Route card with real ETA when a maps adapter is configured (Apple Maps / Google Directions). Leave-by = event start − ETA − buffer. “Where’s Ned?” only if Ned’s **opted-in** share is active (item 39). **Fallback:** keep 30 min estimate and say so (today’s honesty string stays until Maps is live).
- **Wave:** 4 (Maps later-wave). **Depends on:** calendar (exists), 39.

#### 17. Indoor nav with a phone or glasses
- **From:** Glasses / F.R.I.D.A.Y. era
- **Exists:** UX matrix lists AR as future. No indoor graph.
- **Gap:** Building maps, BLE/Wi‑Fi RTT or ARKit world-map, glasses client.
- **Real-time target:** Phone (then glasses) shows turn-by-turn **inside a mapped space the owner created** (home/lab floorplan). Voice: “Take me to the printer.” **Fallback:** room list + “I don’t have an indoor map; here’s the floorplan photo” (47).
- **Wave:** 4–5. **Depends on:** 16’s location, iOS ARKit later-wave.

#### 18. Calendar + buy tickets to keep a group busy
- **From:** E.D.I.T.H. opera tickets
- **Exists:** Google Calendar **read-only**. No create-event in production provider mode. No ticket vendor.
- **Gap:** `calendar:act` create_event is adapter-shaped but not a real Google write. Ticket purchase needs a vendor API + payment — later-wave.
- **Real-time target:** “Put dinner on Friday 7pm” creates a calendar event (after write scope + confirm). “Keep Saturday night free / find tickets for X” searches a ticket adapter and **drafts** a hold; **charge only after explicit confirm**. **Fallback:** create the calendar hold + open a search URL; never silent-buy.
- **Wave:** 3 (calendar write), 5 (paid tickets). **Depends on:** vault/OAuth (exists), 1.

#### 19. Unlock features after a training / onboarding checklist (Training Wheels)
- **From:** Karen Training Wheels Protocol
- **Exists:** Nothing named Training Wheels. Consents exist per training track. Feature flags are implicit.
- **Gap:** A real checklist: mic permission, speaker enroll, quiet hours set, first calibrate, first HUD. Locked tools refuse with “complete Training Wheels.”
- **Real-time target:** New owner is gated. Completing steps unlocks 14, 21, 30, 45, etc. “What is locked?” lists remaining steps. Completing plays item 7. **Never** unlocks futuristic/lethal items.
- **Wave:** 2. **Depends on:** 4, 5, 7.

#### 20. Teach modes of *your* gear
- **From:** Karen web-type tutor
- **Exists:** Tools have names; maker jobs have statuses; no per-device mode catalog with a tutor dialogue.
- **Gap:** Gear registry modes (printer: draft/normal; drone: hover/land; lights: scene names) + “what can this do?”
- **Real-time target:** “What can the printer do?” / “Switch the printer to draft.” Spoken tutor + HUD. Unknown mode refused.
- **Wave:** 2. **Depends on:** 21, 30/13/45 as those adapters land.

#### 21. Voice control of *your* onboard systems (volume, HUD, lights, hobby drone)
- **From:** Karen, J.A.R.V.I.S. — **not Instant Kill**
- **Exists:** Lookout windows, notify helper, fleet tasks (`edith.py`). No unified “set volume / close lookout / land drone” verb set.
- **Gap:** A small, allowlisted actuator vocabulary with confirm on anything that moves hardware.
- **Real-time target:** “Volume down.” “Close the lookout.” “Lights off.” “Land the drone.” Each hits a real actuator or an honest failure. Confirm on drone/lock.
- **Wave:** 3 (software actuators) + 5 (drone). **Depends on:** 11, 13, 45, lookout (exists).

#### 22. Warn that a consumable is empty (parachute analog)
- **From:** Karen parachute
- **Exists:** Maker BOM `reorder_at`; gear scan. Not spoken when filament/battery/first-aid kit is empty.
- **Gap:** Consumable model + callout (10) + quiet hours (9).
- **Real-time target:** Gear or BOM crosses threshold → one spoken warning + HUD. “What am I out of?” lists empties.
- **Wave:** 2. **Depends on:** 10, 9, gear + maker (exist).

#### 23. Go offline / lock when the device is seized or logged out
- **From:** E.D.I.T.H. offline
- **Exists:** Device revoke, push-token delete, identity revoke, voice enrollment revoke. HUD can go stale.
- **Gap:** One **panic / logout** path: revoke tokens, stop ears, lock HUD, remote-wipe local caches, spoken confirmation on a *remaining* trusted device.
- **Real-time target:** “Lock everything” or logout on one device → that device cannot chat or hear; other devices say “Phone A went offline.” Seized-device assumption: owner uses another paired device or master key.
- **Wave:** 5 (complete the last mile). **Depends on:** 11, auth (exists).

#### 24. Hand *your* account to someone else on purpose
- **From:** E.D.I.T.H. user transfer — **accounts, not weapons**
- **Exists:** Single-owner invariant. No delegated “family share” of the assistant.
- **Gap:** A deliberate **delegate** role: time-boxed, scope-boxed (e.g. calendar read only), revocable, audit-logged. Not a second owner by accident.
- **Real-time target:** “Give Ned access to calendar for this weekend.” Ned’s device can list events only. “Revoke Ned.” Immediate. Spoken + compliance log.
- **Wave:** 5. **Depends on:** 11, compliance access-log (exists).

#### 25. Biometric unlock of *your* device
- **From:** E.D.I.T.H. retinal/biometric — **this device only**
- **Exists:** WebAuthn in `identity/webauthn.py`; iOS entitlements / speaker verify for voice. Face ID on iOS app is platform-standard if the app requests it.
- **Gap:** Wire Face ID / Touch ID / WebAuthn as the **gate** for life actions (14, 21, 24) and for web login. Voice verify remains the ears gate.
- **Real-time target:** Opening the iOS app or approving “Call Ned” requires Face ID. Web uses WebAuthn. Failure: no action. **Later-wave:** glasses-mounted biometric if hardware exists; until then phone/Mac biometrics.
- **Wave:** 5. **Depends on:** WebAuthn (exists), iOS later-wave.

---

### Workbench, research, health (26–49)

#### 26. “Check the calibration on this” (sensors, models, printers, radios)
- **From:** E.V. targeting matrix
- **Exists:** `POST /v1/diagnostics/calibrate` for DB/embeddings/gateway/retrieval/storage. Voice-security EER calibration is separate.
- **Gap:** Not invoked by voice. Does not yet include printer/radio/client sensors. Result not a HUD/TTS line.
- **Real-time target:** “Check the calibration.” Runs calibrate, speaks worst check, posts HUD. Extend checks as 30/33/45 come online.
- **Wave:** 2. **Depends on:** diagnostics (exists), 1, 10.

#### 27. Analysis, calibrations, diagnostics on the workbench
- **From:** E.V., J.A.R.V.I.S.
- **Exists:** Same calibrate + `ev checkup` CLI + ops metrics.
- **Gap:** Workbench UI (`/app`, lookout) does not show a live diagnostic strip. No continuous background probe with callouts on change.
- **Real-time target:** Web/Mac workbench always shows last calibrate. Background probe each daemon tick; on flip to failed → callout (10) unless quiet hours.
- **Wave:** 2. **Depends on:** 26, 9, 10.

#### 28. HUD cards on phone, watch, or glasses
- **From:** E.V., Karen, E.D.I.T.H.
- **Exists:** Schemas + Watch complication + `ev card` + lookout/presence HTML + Mac helper `--lookout`.
- **Gap:** Glasses client does not exist. Phone Lock Screen / widget may be partial. Cards are not the default voice response shape.
- **Real-time target:** Every Wave-2+ answer that is a status also emits `ev.hud.card.v1` (or briefing/lookout). Visible on Mac lookout, web, Watch, iOS widget. Glasses = later-wave renderer of the **same** JSON.
- **Wave:** 2 (phone/watch/web/Mac), 5 (glasses). **Depends on:** schemas (exist), 11.

#### 29. Scientific research help with sources
- **From:** E.V., J.A.R.V.I.S.
- **Exists:** `app/ev/research.py` sessions, notes, citations, memory + optional web.
- **Gap:** Not the default voice research path (“Look up X and cite”). Conclusion memories exist; spoken citations may be dropped by the filter.
- **Real-time target:** “Research organic vs synthetic webbing — cite sources.” Opens/continues a research session, speaks a short answer **with citations**, HUD lists URLs. Filter must not strip citations.
- **Wave:** 2. **Depends on:** 1, search (exists), 28.

#### 30. Drive a 3D printer / fabricator
- **From:** E.V. fabricator
- **Exists:** Maker projects, BOM, print jobs **queued in DB**. Status updates are manual API. No OctoPrint/Moonraker.
- **Gap:** Adapter to a real printer; progress telemetry; voice “print the spacer.”
- **Real-time target:** OctoPrint adapter. “Print job spacer.” Confirm → job starts → callouts on start/done/fail. **Fallback:** queue-only + “I have no printer connected” until the adapter is configured.
- **Wave:** 5 (printer later-wave); queue UX in 2. **Depends on:** 20, 22, vault.

#### 31. Suit and desk: same assistant on wearable + workstation
- **From:** E.V., J.A.R.V.I.S.
- **Exists:** Multi-device registry; web workbench; Watch card. Conversation identity not unified (see 11).
- **Gap:** Wearable is mostly HUD, not a conversational peer. Desk (web/Mac) and suit (phone/watch) must share session + memories (already server-side) **and** live transcript.
- **Real-time target:** Ask on Watch/phone, see the answer on the Mac lookout instantly, and vice versa. One conversation id.
- **Wave:** 1–2. **Depends on:** 11, 28.

#### 32. CAD / design assist and “how long will this take”
- **From:** J.A.R.V.I.S. suit render/estimate
- **Exists:** Maker `estimated_minutes` on print jobs. No CAD file understanding.
- **Gap:** Ingest STL/STEP/SVG; estimate time/material via slicer (PrusaSlicer/Cura CLI) or a conservative heuristic; design help via research+files (not generative magic).
- **Real-time target:** Owner drops an STL. EV: “About 42 minutes, 11 g filament.” Voice: “How long will the spacer take?” **Fallback:** owner-entered estimate, labeled as such.
- **Wave:** 5. **Depends on:** 30, 29.

#### 33. Monitor *your* vehicle or drone in a test
- **From:** J.A.R.V.I.S. flight-test (icing analog)
- **Exists:** Gear snapshots. No NMEA/MAVLink/OBD ingest.
- **Gap:** Telemetry endpoint + HUD strip: battery, altitude/speed if provided, weather overlay (37).
- **Real-time target:** During an owner-started “test,” live HUD updates; “how’s the battery?” speaks the last sample. **Fallback:** phone-as-sensor (GPS/baro) labeled “phone stand-in.”
- **Wave:** 5. **Depends on:** 37, 28, 45.

#### 34. Device power / battery / storage
- **From:** J.A.R.V.I.S., Karen
- **Exists:** `gear.scan` probes Mac and reported snapshots.
- **Gap:** Phones/watch must **push** battery/storage on heartbeat. Voice “what’s my battery?”
- **Real-time target:** Heartbeat includes battery/storage. “What’s the phone battery?” speaks it. Low battery → callout (22).
- **Wave:** 2. **Depends on:** 11, 22, gear (exists).

#### 35. Wearable vitals: sleep, heart rate, “you took a hit / you look wrecked”
- **From:** J.A.R.V.I.S., Karen, E.V.
- **Exists:** Health radar readiness, anomalies, morning brief via `POST /v1/health/snapshot`.
- **Gap:** No HealthKit / Health Connect ingest. Morning brief not spoken automatically.
- **Real-time target:** iOS HealthKit later-wave posts snapshots. “How do I look?” speaks readiness + flags. Morning brief at quiet-hours end. **Fallback:** manual/API snapshot (already works) + Watch heart-rate if entitled.
- **Wave:** 2 (API/voice), 5 (HealthKit). **Depends on:** 9, 10, health_radar (exists).

#### 36. Concussion / “you hit your head” — symptom check + see a doctor, not a diagnosis
- **From:** Karen
- **Exists:** Nothing dedicated. Must **not** diagnose.
- **Gap:** A scripted screening: what happened, symptoms list, **always** “I’m not a doctor — get medical care if …” Optional log to health snapshot.
- **Real-time target:** “I hit my head.” EV runs the script, does not claim concussion yes/no, offers to notify an emergency contact (14) if the owner asks.
- **Wave:** 2. **Depends on:** 1, 8, 14 (optional).

#### 37. Weather and local environment from public sensors
- **From:** J.A.R.V.I.S.
- **Exists:** First-class `get_weather` tool (`app/ev/tools.py`) calling `weather_results` in `app/search/live.py`. Open-Meteo geocoding + forecast (no API key): current conditions, 3-day outlook, WMO sky codes. Live search also routes weather queries here (`is_weather_query`). Covered by `backend/tests/test_live_search.py`.
- **Gap:** Not the voice/HUD default. Items 16 (route/leave-by) and 33 (vehicle/drone test) do not yet consume this forecast. Optional: pin owner home lat/lon so “what’s the weather?” works without a place name.
- **Real-time target:** “What’s the weather?” speaks the Open-Meteo snippet and posts `ev.hud.card.v1`. Route briefings (16) and test HUD (33) overlay the same forecast. **Fallback:** already ships — omit place and it uses owner coarse location or asks for a city.
- **Wave:** 2 (voice/HUD + use-in-16/33). **Depends on:** `get_weather` (exists), 1, 10, 28.

#### 38. Brief a team: “here’s the view, here’s the risk”
- **From:** J.A.R.V.I.S. “view from upstairs”
- **Exists:** `POST /v1/tactical/brief`, `ev.hud.briefing.v1`, lookout ops.
- **Gap:** Not voice-default. Not shareable to opted-in teammates (24/39).
- **Real-time target:** “Brief me.” Speaks a short brief; HUD briefing card. “Send this brief to Ned” only if Ned is a delegate (24).
- **Wave:** 2. **Depends on:** tactical (exists), 28, 1.

#### 39. Locate teammates who **opted in**
- **From:** J.A.R.V.I.S., Karen
- **Exists:** `GET /v1/people/{name}/whereabouts` = memory last-seen, **not** live GPS.
- **Gap:** Opt-in live share (Find My / OS location share ingested with consent). Must never hunt strangers.
- **Real-time target:** Teammate enables share → “Where’s Ned?” speaks last ping + map card. If not opted in: “I only have memory: last mentioned at …”
- **Wave:** 4. **Depends on:** 16, people (exists), 24.

#### 40. Replay **your** cameras at **your** place
- **From:** E.V. apartment cam
- **Exists:** Vision attachment analysis; no NVR/HomeKit camera pull.
- **Gap:** Adapter to owner RTSP/HomeKit Secure Video. Clip fetch + “what happened at 16:00.”
- **Real-time target:** “Show the lab camera from 4pm.” HUD/web plays **owner** footage. Voice summary via existing vision analyze. **Fallback:** owner uploads a clip.
- **Wave:** 5. **Depends on:** 28, vision (exists). **Never** other people’s cameras.

#### 41. Public crime / news / scanner-style alerts you subscribed to
- **From:** E.V. / Polygon crime alerts
- **Exists:** Alert radar watchlist over **owner** events/deadlines. Live search exists. Not RSS/NWS/police **public** feeds.
- **Gap:** Subscription sources: RSS, public alerts, optional Broadcastify-class **public** streams the owner picks. Quiet hours apply.
- **Real-time target:** “Subscribe to NWS alerts for my county.” Hits become callouts (10). “What’s on the scanner digest?” reads the digest. No city-wide private CCTV.
- **Wave:** 2 (watchlist voice) + 4 (public feed adapters). **Depends on:** 9, 10, alert_radar (exists).

#### 42. “Is this video likely fake?” at best-effort accuracy
- **From:** E.D.I.T.H. “not an illusion”
- **Exists:** Vision/OCR/scene on attachments. No deepfake score.
- **Gap:** Best-effort heuristics (metadata, known-generator artifacts, optional model) with **never claim certainty**.
- **Real-time target:** Owner shares a video. EV: “Likely edited / inconclusive / no artifacts I know — this is not proof.” HUD shows reasons.
- **Wave:** 4. **Depends on:** vision (exists), 29.

#### 43. Voice changer as a joke / accessibility filter
- **From:** Karen interrogation mode — **audio FX only**
- **Exists:** Kokoro/hosted TTS voices. No live pitch/FX on **owner playback** or TTS persona switch beyond model voice.
- **Gap:** TTS voice picker + optional playback FX. **Not** an intimidation protocol; UI copy forbids that framing.
- **Real-time target:** “Use the lower voice.” Subsequent TTS uses that voice until reset. Accessibility: slower rate, higher contrast TTS. No “interrogation mode” string.
- **Wave:** 4. **Depends on:** 1, 2.

#### 44. Look up **public** records where the law allows
- **From:** Karen / Gargan dossier — **public data only**
- **Exists:** Web search. No structured public-records adapter.
- **Gap:** Allowlisted public sources (SEC, court PACER-public, company registries, Wikipedia). Refuse non-public / dox requests.
- **Real-time target:** “What’s public about {company}?” cites public pages. “Get me that person’s private number” is refused.
- **Wave:** 4. **Depends on:** 29, search (exists), output filter (exists).

#### 45. A small recon drone **you own**, on a leash
- **From:** Karen “Droney”
- **Exists:** Fleet tasks conceptually; no drone SDK.
- **Gap:** Owner-paired drone (DJI/Tello/MAVLink). Geofence = owner property / LOS. Confirm on takeoff. No weapons.
- **Real-time target:** “Take off and hover.” / “Land.” Video optional in lookout. **Fallback:** simulated drone in local mode for CI + HUD.
- **Wave:** 5. **Depends on:** 21, 33, 19 (locked until Training Wheels).

#### 46. Track a **beacon you planted on your own gear**
- **From:** Karen spider-tracer — **own gear only**
- **Exists:** Device registry for EV clients, not AirTags.
- **Gap:** Ingest Find My / Chipolo / generic BLE beacon the owner registers as *their* asset.
- **Real-time target:** “Where’s my backpack tag?” map card. Refuse unlabeled people-tracking. **Fallback:** last-seen of registered EV devices only.
- **Wave:** 5. **Depends on:** 16, 11.

#### 47. Rough structure estimates from photos, maps, or known plans
- **From:** Karen ferry X-ray **idea** — slow, approximate, not live FEA
- **Exists:** Vision on attachments. Navigation notes.
- **Gap:** A “measure / estimate” tool: owner photo or floorplan → rough distances, “this beam looks like the load path” as **guess**, with disclaimer.
- **Real-time target:** Owner photos a shelf. EV: “About 80 cm wide if the bottle is 20 cm — low confidence.” Never “X-ray complete, 98%.”
- **Wave:** 4. **Depends on:** 29, 32 (optional), vision.

#### 48. Quiet “something’s off” alerts from **your** patterns
- **From:** E.V. spider-sense analog
- **Exists:** EV Sense predictions + attention policy + health anomalies + calendar signals + companionship isolation.
- **Gap:** One fused “sense” pass on daemon tick; spoken only when policy allows; inspectable “why now?”
- **Real-time target:** EV notices missed sleep + three late deadlines + isolation → one quiet card, not a barrage. “Why did you ping me?” cites sources. Owner can dismiss.
- **Wave:** 2. **Depends on:** 9, 10, 15, 35, 41, ev_sense (exists).

#### 49. Company / calendar / inbox help
- **From:** J.A.R.V.I.S. “runs more of the business than anyone besides Pepper” — **assistant, not CFO**
- **Exists:** Calendar read, GitHub adapter, mail.list/send via LifeHelper, search.
- **Gap:** Unified “what’s on my plate” across calendar + mail + GitHub + watchlist. Draft replies, don’t silently send. No autonomous company control.
- **Real-time target:** “What’s on my plate?” spoken digest. “Draft a reply to X.” Owner approves send (mail.send evidence). GitHub: list PRs / issues the owner can already read.
- **Wave:** 3. **Depends on:** 18, LifeHelper mail (exists), GitHub adapter (exists), 9.

---

## Real-time integration contract (all waves)

Every item that claims “working in real time” must, when implemented later:

1. Be reachable by **voice** (wake session) **or** an explicit HUD/control the owner is already looking at.
2. Use a **shipped** function (API + service), not a markdown checkbox.
3. Honor **quiet hours** if it is proactive (9).
4. Leave an **audit/access-log** row for actuators (13, 14, 18, 21, 24, 30, 45).
5. Fail **honestly** when the vendor/hardware is missing (say so; use local double only in tests).
6. Have a test that drives the **real entry point** (voice tool or HTTP handler), not a restated stub.

CI always runs `local` doubles. Owner machines enable real providers via env + vault, same as Calendar/GitHub today.

---

## Suggested later PRs (implementation is a future goal)

| PR | Title | Items | Depends on |
|---|---|---|---|
| P0 | Session + protocol sheet + nickname | 1–6, 11, 31 | — |
| P1 | Dedication, Training Wheels, quiet-hours on voice proactive | 7–10, 19 | P0 |
| P2 | Voice-wire existing calibrate/gear/health/weather/brief/sense/research/HUD | 12, 20, 22, 26–29, 34–38, 48 | P1 |
| P3 | Life I/O: calls, timers, calendar write, inbox digest | 14, 15, 18, 49 | P1 |
| P4 | Smart-home adapter live + software actuators | 13, 21 | P3 |
| P5 | Maps, opted-in location, public feeds, records, estimates, deepfake-heuristics, TTS voices | 16, 17, 39, 41–44, 47 | P2 |
| P6 | Panic lock, delegate access, biometrics gate | 23–25 | P0 |
| P7 | OctoPrint + CAD estimate | 30, 32 | P2 |
| P8 | Owner cameras, beacons, vehicle/drone telemetry + leashed drone | 33, 40, 45, 46 | P4, P5 |

No PR in this list implements a futuristic §2 item.

---

## Risks

- **Scope:** 49 real-time features is many PRs. Waves exist so the voice trunk ships before drones.
- **Vendor lock / keys:** Maps, tickets, HealthKit, drones need owner accounts. Fallbacks keep CI green.
- **Single-owner vs item 24:** Delegate must stay narrower than owner or the identity model breaks.
- **Medical/legal:** 36 and 44 are scripted and refused where required; never “diagnosis” or doxxing.
- **This goal does not ship behavior.** Implementation is a later authorized goal.

---

## Sources for the 49 titles

`docs/MCU_AI_ASSISTANTS.md` §1 Buildable tables (voice 1–10, house 11–25, workbench 26–49). Movie analogs as cited there. Repo surfaces as cited in “What already exists.”
