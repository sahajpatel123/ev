# BUILDABLE_DETAILED — technical design for items 1–49

Elaboration of [`docs/BUILDABLE_FEATURES_PLAN.md`](BUILDABLE_FEATURES_PLAN.md). Same item numbers and capabilities. This file is **how to implement later**, not implementation. Parent plan stays the numbered source of truth.

**Audience:** a developer or architect who already knows this repo. Each item names real modules, the plug-in point on the voice/HUD/tool path, data to add, and how to fail honestly.

**Not in this file as work to build:** Instant Kill / lethal modes; telecom backdoors / reading strangers’ texts; city-scale cameras or facial hunt; satellite / combat-drone weapons; House Party armor swarm; becoming Vision; telepathy math; organic-web DNA scans; omni-hack; Baby Monitor used to ID strangers. Those stay **refusals** on the protocol sheet (item 5). See `docs/MCU_AI_ASSISTANTS.md` §2.

---

## Shared system design

Almost every item is a **tool**, a **HUD card**, or an **attention-policy gate** on the existing loop. Do not ship 49 screens.

```text
Owner audio/text
    → wake/verify (app/voice/lifecycle.py)     [items 1, 6, 25]
    → ASR (optional) → conversation thread     [items 1, 11, 31]
    → gateway + tool_loop (app/services/tool_loop.py)
         → app/ev/tools.py registry            [14, 26, 29, 37, …]
         → IntegrationRegistry adapters        [13, 16, 18, 30, 40, 41, 45]
    → output filter (must keep citations)      [29, 42, 44]
    → TTS + ev.hud.* emit                      [10, 28]
    → notify.policy / ev_sense quiet hours     [9, 10, 22, 48]
```

### Contracts every implementer reuses

| Contract | Where | Rule |
|---|---|---|
| Voice session | `VoiceSession`, `/v1/voice/*` | One wake opens `awake`; sleep phrases end it. Ambient speech is `403 voice_ignored`. |
| Conversation | `ConversationThread` via `app/ev/conversation.py` | Cross-device items (11, 31) share **one** `conversation_id`. Voice already has `voice_continuity_conversation_id`. |
| Tools | `TOOL_SPECS` + dispatcher in `app/ev/tools.py` | Declare name, JSON schema, `permission`, `read_only`, `sensitive`. Dispatcher validates I/O and `log_access`. |
| Life evidence | `life_helper.py` | `call.place` / `mail.send` / `messages.send` succeed only on helper evidence (`opened` / `sent`). Same honesty for locks, prints, drones. |
| HUD | `docs/schemas/ev-hud-*.json`, `app/ev/hud.py` | Status answers emit `ev.hud.card.v1` (or route/briefing/lookout). Clients only render JSON. |
| Attention | `app/notify/policy.py`, `quiet_hours_active()` | Proactive speech **and** push go through `decide()`. Emergency pierce only. |
| Fleet | `docs/WAVE_LIFE.md`, `app/notify/routing.py` | Actuators route to a capable, reachable, non-revoked device. |
| Feature flags | **new** table, item 19 | `enabled` / `needs_setup` / `locked` / `refused`. Futuristic = `refused` forever. |
| Adapters | `app/integrations/adapters.py` | `local` double in CI; real `provider` on the owner box. Weather is **not** a new adapter. |
| Audit | `app/services/access_log.py` | Required for 13, 14, 18, 21, 24, 30, 45. |

### New shared types (Wave 0 — add once, used by many items)

```text
AssistantProfile
  nickname: str | null          # item 2
  owner_preferred_name: str     # item 6
  tts_voice_id: str             # item 43
  dedication_text / blob_id     # item 7
  greeting_enabled: bool

FeatureGate
  key: str                      # e.g. life.call, actuator.drone
  state: locked|enabled|needs_setup|refused
  unlocks_after: [training_step]
  refused_reason: str | null    # futuristic

Callout
  id, created_at, text, hud_schema, source_item
  spoken: bool                  # item 10 replay

OwnerPrefs (synced on pair)     # item 12
  nickname, quiet_hours, hud_layout, feature_gates, tts_voice_id

Timer
  fire_at, text, conversation_id
  survives daemon restart       # item 15
```

### Real-time bar (copied from parent, binding here)

1. Reachable by **voice** or a HUD the owner is already looking at.
2. Shipped function (API + service), not a checkbox.
3. Quiet hours if proactive.
4. Audit row for actuators.
5. Honest failure when hardware/vendor is missing (`local` doubles only in tests).
6. Test drives the **real** tool or HTTP handler.

### Waves (unchanged)

Wave 0 substrate · Wave 1 items 1–10 · Wave 2 close existing APIs to voice/HUD (incl. **37**) · Wave 3 life I/O · Wave 4 place/people/media · Wave 5 hardware later-wave.

---

## Voice, personality, companionship

#### 1. A named voice companion you talk to all day

- **From:** J.A.R.V.I.S., Karen, E.D.I.T.H., E.V.
- **Exists:** `app/voice/lifecycle.py` state machine (idle → verify → awake → process → respond → follow_up). Sleep phrases. `docs/VOICE.md`. ASR/TTS providers. `VoiceSession` rows.
- **Gap:** Three mouths (Mac ears, iOS, web) do not share one conversation identity. Feels like “POST /utterance,” not a day-long companion. No owner-chosen spoken name (2).
- **Technical design:**
  1. On successful verify, bind `VoiceSession.conversation_id` to `settings.voice_continuity_conversation_id` or create one thread and persist it as the owner’s **live thread**.
  2. `/v1/voice/utterance`, `ev ask`, and web chat all pass that `conversation_id` into `app/ev/conversation.py`.
  3. Follow-up window already exists (`EV_VOICE_FOLLOW_UP_SECONDS`). Do not require a second wake. Sleep phrases stay the only clean exit.
  4. TTS plays on the device selected by `notify/routing.py` (`attention` capability) so Mac and phone are one voice (11).
  5. Inject `AssistantProfile.nickname` (2) and `identity_block()` (3) into every system prompt.
- **Real-time target:** “EVIE” → talk → hear TTS → continue without re-wake → “that’s all” stops. Same thread on another paired device.
- **Wave:** 1 (trunk). **Fallback:** text chat if mic/TTS missing; still one thread.
- **Depends on:** voice lifecycle (exists).

#### 2. Accept a nickname (“Suit Lady” → “Karen”)

- **From:** Karen
- **Exists:** `PersonalityProfile` sliders. People have `display_name`. Assistant has no stored spoken name.
- **Gap:** No `AssistantProfile.nickname`; no tool to set it; TTS/prompt still say “EVIE.”
- **Technical design:**
  1. Table `assistant_profile` (singleton, owner-scoped) with `nickname`, `updated_at`.
  2. Tool `set_assistant_name` `{name}` / `reset_assistant_name`. Permission `assistant:profile`. Not sensitive.
  3. `identity_block()` prefixes: `You are {nickname or "EVIE"}`. TTS greeting uses the same string.
  4. Persist via item 12 so a new device is already “Evie.”
  5. Output filter: refuse names that impersonate the owner or a third party as if EV were them.
- **Real-time target:** “Call yourself Evie.” Next turn answers as Evie. “Go back to EVIE” resets.
- **Wave:** 1. **Fallback:** none needed; default “EVIE.”
- **Depends on:** 1.

#### 3. Dry, loyal personality; “I may be malfunctioning”

- **From:** J.A.R.V.I.S.
- **Exists:** `app/ev/personality.py` versioned sliders + `identity_block`. `docs/UX.md` tone. `POST /v1/diagnostics/calibrate`.
- **Gap:** Sliders not forced on every live reply. No one-shot spoken self-status when last calibrate is red.
- **Technical design:**
  1. Every chat/voice turn loads `get_current()` and compiles `identity_block` (already the hook). Pin `humor`/`formality` so the gateway cannot ignore them — put the block in the **immutable** system prefix, not a suggestion.
  2. Cache last `CalibrationReport` (item 26). On session start, if any check is `failed`/`degraded`, enqueue **one** callout (10): “I may be malfunctioning: {worst.name}.”
  3. Tool `update_personality` already has HTTP; add voice mapping. Next completion must reflect the change (eval: same prompt, different slider → different length/tone).
  4. Never fake green: if gateway is down, say so; do not invent a witty answer.
- **Real-time target:** Slider change audible next turn. Red calibrate spoken once per session.
- **Wave:** 1. **Fallback:** if calibrate never ran, skip the self-status line.
- **Depends on:** 1, 26.

#### 4. Introduce yourself and explain what you can do

- **From:** E.D.I.T.H.
- **Exists:** Docs. No first-run spoken tour bound to live gates.
- **Gap:** Onboarding dialogue + HUD list of **unlocked** protocols (5, 19).
- **Technical design:**
  1. Flag `assistant_profile.onboarding_completed_at`.
  2. First wake after install, or intent “what can you do,” calls `protocol_sheet()` (5) and speaks ≤8 bullets of `enabled` items, then “say start training wheels” (19).
  3. Emit `ev.hud.card.v1` title “Protocols” body = same list.
  4. Do not list `refused` unless asked (5).
- **Real-time target:** First wake / “What can you do?” = honest unlocked list + HUD.
- **Wave:** 1. **Fallback:** CLI `ev protocols`.
- **Depends on:** 5, 19.

#### 5. “Not built only for you — you have these protocols” (honest capability list)

- **From:** E.D.I.T.H.
- **Exists:** `TOOL_SPECS`, integration scopes. Not owner-facing.
- **Gap:** Live protocol sheet: `enabled` / `needs_setup` / `locked` / `refused`.
- **Technical design:**
  1. `GET /v1/assistant/protocols` builds from `FeatureGate` + adapter health (vault token present? printer reachable?).
  2. Hard-code `refused`: Instant Kill, telecom wiretaps, city facial hunt, satellite/drone weapons, becoming Vision, stranger Baby Monitor. Copy stays explicit.
  3. Tool `list_protocols` `{filter?}`. Voice: “What protocols do I have?”
  4. `needs_setup` points at the env/adapter (e.g. “OctoPrint URL unset”).
- **Real-time target:** Spoken + HUD three-column list. Owner hears the refusals when they ask for a banned thing.
- **Wave:** 1. **Fallback:** static refused list even if gates table empty.
- **Depends on:** 19.

#### 6. Greet the current user by name; welcome them back

- **From:** E.D.I.T.H.
- **Exists:** Speaker verify = owner vs not. Identity/people names.
- **Gap:** No welcome line; verify failure already silent (`403`).
- **Technical design:**
  1. `AssistantProfile.owner_preferred_name` (default from identity display name).
  2. On `awake` transition only, if `greeting_enabled`, TTS one sentence. Do not greet on every follow-up utterance.
  3. Text `/v1/chat` first message in a new thread: same line as a system-side assistant event, not a fake user turn.
  4. Verify fail: **no** greeting, no LLM. Keep `voice_ignored`.
- **Real-time target:** “Welcome back, {name}.” after verify. Impostor hears nothing.
- **Wave:** 1. **Fallback:** skip if name unset (“Welcome back.”).
- **Depends on:** 1, speaker enrollment.

#### 7. Play a short trust / dedication message from the person who set it up

- **From:** E.D.I.T.H. (“For the next Tony Stark, I trust you.”)
- **Exists:** Nothing.
- **Gap:** Stored note + play-once + on-demand.
- **Technical design:**
  1. `dedication_text` (≤500 chars) and/or `dedication_blob_id` (≤30s audio in object store).
  2. Tool `set_dedication` / `play_dedication`. Play uses TTS if text, else stream blob through the same playback path as replies.
  3. After Training Wheels completes (19), fire `play_dedication` once; set `dedication_played_at`.
  4. Not proactive later; only on request or that one unlock.
- **Real-time target:** Record/type note; first unlock plays it; “Play the dedication” later.
- **Wave:** 1. **Fallback:** text-only if no mic for recording.
- **Depends on:** 1, 19.

#### 8. Love / social advice, vault-night small talk

- **From:** Karen, E.V.
- **Exists:** LLM chat. `app/ev/companionship.py` isolation scan + relationship stats.
- **Gap:** Isolation scan is API-only. Need a **mode** with a time budget and anti-dependency copy (`docs/EVIE_RESEARCH.md`).
- **Technical design:**
  1. Interaction mode `social` (see `app/ev/interaction.py` modes) with verbosity cap and “do not claim to be the only friend.”
  2. After N social turns or when `scan_isolation` trips, **one** spoken nudge naming a real person from memory (people store). Then stop.
  3. Quiet hours (9) still apply to that nudge.
  4. Filter: refuse sexual-companion / romantic-replacement framing.
- **Real-time target:** Owner can chat. Isolation → one honest human-connection line, not “I’m your only friend.”
- **Wave:** 1. **Fallback:** scan never runs → no nudge (better than a fake one).
- **Depends on:** 1, 9, companionship.

#### 9. Stay out of the way unless needed (quiet hours / attention budget)

- **From:** E.V. (less intrusive than Karen)
- **Exists:** `quiet_hours_active()`, `notify/policy.py` (dedup, daily cap, emergency pierce), `ev_sense.apply_attention_policy`.
- **Gap:** Spoken proactive (10, 22, 48) and lookout auto-open ignore quiet hours.
- **Technical design:**
  1. Single function `may_speak_proactive(session, *, emergency) -> PolicyDecision` wrapping `decide()` + `quiet_hours_active()`.
  2. Callout bus (10), consumable warnings (22), sense (48), morning brief (35) **must** call it.
  3. Tool `set_quiet_hours` `{until}` / `{start,end}` updates `settings`/prefs immediately (in-process + persist).
  4. At quiet-hours end, daemon builds digest (already `build_digest`) and may speak **one** summary if policy allows.
  5. Lookout `compose_and_maybe_open` checks the same gate before auto-open.
- **Real-time target:** “Go quiet until 8.” No speak/push until then except emergency. Digest after.
- **Wave:** 1 (API) + 2 (all sources honor it). **Fallback:** if clock TZ missing, treat as quiet-hours **on** (fail closed).
- **Depends on:** settings.

#### 10. Status callouts and “what just happened” narration

- **From:** E.V., Karen
- **Exists:** Lookout compose, notification titles. No event→speech bus.
- **Gap:** `Callout` log + speak-if-allowed + replay.
- **Technical design:**
  1. Table `callouts` (text, source_item, hud payload, spoken bool).
  2. `emit_callout(text, hud, source, *, emergency=False)`: write row → `may_speak_proactive` → TTS if allowed → always store for replay.
  3. Hook: calibrate done (26), print job (30), timer (15), battery (22/34), calendar T-15 (18).
  4. Tool `list_callouts` `{limit}`. Voice: “What just happened?”
- **Real-time target:** Event → one sentence (if allowed) + HUD. Replay last N.
- **Wave:** 2. **Fallback:** HUD-only if TTS down.
- **Depends on:** 1, 9, HUD.

---

## House, lab, devices

#### 11. One voice for home, workshop, and phone

- **From:** J.A.R.V.I.S.
- **Exists:** Device registry, heartbeats, APNs, routing (`WAVE_LIFE.md`). Separate voice stacks per client.
- **Gap:** No shared live thread + attention-routed TTS.
- **Technical design:**
  1. Owner live `conversation_id` (item 1) stored on owner prefs, not per device.
  2. WebSocket or SSE `/v1/runtime/transcript` streams new events for that thread to web/Mac lookout.
  3. TTS playback device = `routing.best(capability=attention|voice)`.
  4. Heartbeat already proves reachability; do not invent a second presence system.
- **Real-time target:** Start on Mac, continue on phone; web shows transcript live.
- **Wave:** 0–1. **Fallback:** if only one device online, all audio stays there.
- **Depends on:** registry, 1.

#### 12. Import preferences onto a new device (“we’re online”)

- **From:** J.A.R.V.I.S.
- **Exists:** Pairing token; server-side memory/personality already shared.
- **Gap:** Client prefs bundle + spoken “we’re online.”
- **Technical design:**
  1. `GET /v1/devices/{id}/bootstrap` returns `OwnerPrefs` (nickname, quiet hours, HUD layout, feature gates, TTS voice).
  2. iOS/Mac call it once after `POST /v1/devices`.
  3. After first successful bootstrap, one TTS on **that** device: “We’re online.” Idempotent flag `bootstrapped_spoken_at`.
- **Real-time target:** Pair phone → prefs land → one spoken line.
- **Wave:** 2. **Fallback:** defaults if bootstrap fails; say “I couldn’t load prefs; using defaults.”
- **Depends on:** 2, 9, 19, 11.

#### 13. Lights, locks, security, “is the garage closed”

- **From:** J.A.R.V.I.S.
- **Exists:** `smart_home` adapter, local double + HTTP passthrough. Vault.
- **Gap:** No Home Assistant/HomeKit provider, no entity inventory, no evidence-backed act.
- **Technical design:**
  1. Adapter `provider=homeassistant`: REST `/api/states`, `/api/services`. Token in vault. `provider=homekit` later via a helper (TCC).
  2. Actions: `light.set`, `lock.set`, `cover.set`, `home.status`. Success only if provider returns new state matching the request (same honesty as `call.place`).
  3. Tools `home_status` `{area?}` (read_only), `home_act` `{entity, action}` (`sensitive`, confirm on lock).
  4. Inventory sync → `GearSnapshot` or a `home_entities` table so “lab lights” resolves.
  5. Audit every `home_act`.
- **Real-time target:** “Are the lab lights on?” / “Lock the front door” with evidence.
- **Wave:** 3. **Fallback:** `local` double in CI; web toggles as fake house until hub configured — spoken as “simulated home.”
- **Depends on:** 11, 21, vault.

#### 14. Place calls / “try this person”

- **From:** J.A.R.V.I.S., Karen
- **Exists:** `EVLifeHelper call.place`, `phone.call` / `facetime.call`, `contacts.resolve`, iOS CallKit manager.
- **Gap:** Not a voice tool with spoken evidence.
- **Technical design:**
  1. Tool `place_call` `{name|destination, kind: tel|facetime}` already sketched in tools/life bridges — bind it to voice intents.
  2. Resolve: `contacts.resolve` then people-memory display name. **Owner address book only.**
  3. Confirm if autonomy ≠ full (`actions.py` `autonomy_mode`).
  4. Speak “Ringing {name}” **only if** helper `data.opened == true`. Else speak the error code.
  5. Gate with Training Wheels (19) + biometrics (25) when those land.
- **Real-time target:** “Call Ned.” Confirm → ring only on evidence.
- **Wave:** 3. **Fallback:** “Calling isn’t available on this device” if helper exit 4.
- **Depends on:** 1, LifeHelper, contacts.

#### 15. Timekeeping, reminders, “37 minutes have passed”

- **From:** Karen
- **Exists:** Calendar signals, routines, watchlist. No arbitrary timer.
- **Gap:** Durable timers + spoken fire.
- **Technical design:**
  1. Table `timers` (`fire_at`, `payload`, `status`). Daemon tick (`runtime_daemon.py`) due-scans.
  2. Tools `start_timer` `{minutes|at, text}`, `session_elapsed` (from `VoiceSession` start).
  3. On fire: `emit_callout` (10) gated by 9.
  4. Persist across process restart (DB, not in-memory).
- **Real-time target:** “Remind me in 37 minutes.” / “How long have I been here?”
- **Wave:** 2–3. **Fallback:** if daemon down, timers still stored; fire late with “this is late.”
- **Depends on:** 9, 10, daemon.

#### 16. Fastest route, leave-by, “friends are at X” if they share location

- **From:** Karen, J.A.R.V.I.S.
- **Exists:** `app/ev/navigation.py` + `ev.hud.route.v1` + **hardcoded 30 min**. People whereabouts = memory.
- **Gap:** Maps ETA adapter. Opted-in live share is item 39.
- **Technical design:**
  1. Adapter `maps` (`local` returns 30 + `estimate=true`; `google` Directions or Apple later).
  2. `route_briefing` calls maps with origin = owner coarse location, dest = calendar location. `leave_by = start - eta - buffer`.
  3. Overlay `get_weather` (37) as a note (“rain, add 5 min”) — **consume existing weather**, do not add a provider.
  4. “Where’s Ned?” branches: live share (39) vs memory last-seen. Never mix.
  5. Honesty string stays until maps configured.
- **Real-time target:** Real ETA card when maps live; otherwise 30 min **said as estimate**.
- **Wave:** 4. **Fallback:** current 30 min stub.
- **Depends on:** calendar, 37 (exists), 39.

#### 17. Indoor nav with a phone or glasses

- **From:** Glasses / F.R.I.D.A.Y. era
- **Exists:** UX AR = future. No indoor graph.
- **Gap:** Owner-authored floorplan + on-device ARKit later.
- **Technical design:**
  1. `indoor_maps` : graph of rooms/nodes the owner draws (web editor) + photo attachments (47).
  2. Tool `indoor_route` `{to_room}`. If no graph: “I don’t have an indoor map” + show photo.
  3. iOS later: ARKit world map / BLE beacons for pose; render same graph. Glasses consume `ev.hud.route.v1` plus step list — **same JSON**, new renderer (28).
  4. Never claim IR/magic through walls.
- **Real-time target:** “Take me to the printer” → steps or honest no-map.
- **Wave:** 4–5. **Fallback:** room list + floorplan photo.
- **Depends on:** 16, 47, 28.

#### 18. Calendar + buy tickets to keep a group busy

- **From:** E.D.I.T.H. opera tickets
- **Exists:** Google Calendar **read-only** + PKCE vault. `calendar.create_event` action is declared, not live-written.
- **Gap:** Write scope + confirm. Ticket vendor later-wave.
- **Technical design:**
  1. Calendar adapter: add write scope only after owner OAuth re-consent. `calendar.create_event` hits `calendar/v3` POST; success = event `id` in evidence.
  2. Tool `calendar_add` `{title, start, end, location?}` — confirm unless autonomy full.
  3. Ticket adapter (Wave 5): search-only first; `ticket_hold` drafts URL + price; `ticket_buy` **never** without explicit confirm + payment token. Default: open search URL, create calendar hold.
  4. Audit both write and any buy attempt.
- **Real-time target:** “Dinner Friday 7” creates event. Tickets = draft/search, never silent-buy.
- **Wave:** 3 write, 5 paid. **Fallback:** calendar hold + browser search.
- **Depends on:** vault, 1.

#### 19. Unlock features after a training / onboarding checklist (Training Wheels)

- **From:** Karen Training Wheels
- **Exists:** Training consents. No named checklist / feature gates.
- **Gap:** `FeatureGate` + steps + refuse locked tools.
- **Technical design:**
  1. Steps: mic permission, speaker enroll, quiet hours set, first calibrate (26), first HUD shown (28).
  2. Dispatcher: if tool.permission maps to a `locked` gate → return `{ok:false, error: training_wheels, remaining: [...]}` — do not call the adapter.
  3. Completing last step: set gates `enabled` for 14, 21 (software), 30 queue; play dedication (7).
  4. **Never** a gate that unlocks Instant Kill or any §2 item — those are `refused` in the seed migration.
  5. Voice: “What is locked?” / “Start training wheels.”
- **Real-time target:** New owner gated; complete steps; lethal/futuristic stay refused.
- **Wave:** 2. **Fallback:** if gates table missing, fail closed on actuators, open on read-only tools.
- **Depends on:** 4, 5, 7.

#### 20. Teach modes of *your* gear

- **From:** Karen web-type tutor
- **Exists:** Tool names, maker statuses. No per-device mode catalog.
- **Gap:** Mode dictionary + tutor dialogue.
- **Technical design:**
  1. `gear_modes(device_id) -> [{name, description, current}]` provided by each adapter (printer, lights, drone).
  2. Tools `gear_explain` `{device}`, `gear_set_mode` `{device, mode}`.
  3. Unknown mode → refuse with the valid list (Karen-style tutor, not a crash).
  4. HUD card lists modes; current highlighted.
- **Real-time target:** “What can the printer do?” / “Switch to draft.”
- **Wave:** 2. **Fallback:** empty catalog → “that device has no modes yet.”
- **Depends on:** 21, adapters 13/30/45 as they land.

#### 21. Voice control of *your* onboard systems (volume, HUD, lights, hobby drone)

- **From:** Karen, J.A.R.V.I.S. — **not Instant Kill**
- **Exists:** Lookout windows, fleet tasks (`app/ev/edith.py`). No allowlisted verb set.
- **Gap:** Small actuator vocabulary + confirm on hardware.
- **Technical design:**
  1. Allowlist: `volume.set`, `lookout.close|open`, `hud.card`, `home_act` (13), `drone.cmd` (45). **No** kill/weapon verbs — reject at parse.
  2. Tool `actuate` `{verb, args}` → switch to the real module. Hardware verbs require confirm + Training Wheels + audit.
  3. Software verbs (volume, close lookout) need no confirm.
  4. Feature gate `actuator.drone` stays locked until 19 + 45 configured.
- **Real-time target:** “Volume down.” “Close the lookout.” “Lights off.” “Land the drone.” Honest failure otherwise.
- **Wave:** 3 software, 5 drone. **Fallback:** verb not available → speak the allowlist.
- **Depends on:** 11, 13, 45, lookout.

#### 22. Warn that a consumable is empty (parachute analog)

- **From:** Karen parachute
- **Exists:** Maker BOM `reorder_at`; gear scan.
- **Gap:** Threshold → callout.
- **Technical design:**
  1. On gear scan / BOM change, if `qty <= reorder_at` or battery `<` threshold, `emit_callout` source=22.
  2. Tool `list_empties`. Dedup via notify fingerprint so it does not nag every tick.
  3. Quiet hours apply.
- **Real-time target:** Threshold → one spoken + HUD. “What am I out of?”
- **Wave:** 2. **Fallback:** no thresholds set → no warnings.
- **Depends on:** 10, 9, gear, maker.

#### 23. Go offline / lock when the device is seized or logged out

- **From:** E.D.I.T.H. offline
- **Exists:** Device revoke, push delete, identity/voice revoke.
- **Gap:** One panic path + ears stop + HUD lock + wipe local caches.
- **Technical design:**
  1. `POST /v1/devices/{id}/panic` and `POST /v1/runtime/lock-all` (master key).
  2. Effects: `revoked_at`, stop ears process (launchd/client honor revoke on next heartbeat), clear client token store, lookout shows “offline.”
  3. Remaining trusted device gets callout “Phone A went offline.”
  4. Voice “Lock everything” on a **still-trusted** device. Seized device cannot be trusted to obey; owner uses another device or master key — document that honestly.
- **Real-time target:** Panic → that device cannot chat/hear; others announced.
- **Wave:** 5. **Fallback:** revoke token only if client ignore (still blocks API).
- **Depends on:** 11, auth.

#### 24. Hand *your* account to someone else on purpose

- **From:** E.D.I.T.H. user transfer — **accounts, not weapons**
- **Exists:** Single-owner invariant. Compliance access-log.
- **Gap:** Time/scope-boxed delegate, not a second owner.
- **Technical design:**
  1. Table `delegates` (`person_id`, `scopes[]`, `not_after`, `revoked_at`). Scopes ⊂ `{calendar:read, research:read, briefing:read}` — **never** life.call, home.lock, drone, panic.
  2. Delegate devices get `trust_level=device` + scope claim on the JWT/token.
  3. Dispatcher enforces scopes. Owner tools `delegate_grant` / `delegate_revoke`.
  4. Audit every grant/use/revoke.
- **Real-time target:** “Give Ned calendar this weekend.” / “Revoke Ned.”
- **Wave:** 5. **Fallback:** if unimplemented, refuse “share my account” rather than widening owner.
- **Depends on:** 11, access-log.

#### 25. Biometric unlock of *your* device

- **From:** E.D.I.T.H. — **this device only**
- **Exists:** `app/identity/webauthn.py`; speaker verify for ears.
- **Gap:** Face ID / WebAuthn as gate for life actions and web login.
- **Technical design:**
  1. Web: WebAuthn ceremony already fail-closed — require it for `/app` login.
  2. iOS: `LAContext` before `place_call`, `home_act` lock, `delegate_grant`. Failure = no request.
  3. Ears stay speaker-verify (not Face ID on a headless Mac mic).
  4. Glasses biometric = later-wave; until then phone/Mac.
- **Real-time target:** Face ID / WebAuthn required to approve “Call Ned.” Failure = no action.
- **Wave:** 5. **Fallback:** speaker verify + master key on Mac.
- **Depends on:** WebAuthn, iOS later-wave.

---

## Workbench, research, health

#### 26. “Check the calibration on this” (sensors, models, printers, radios)

- **From:** E.V. targeting matrix
- **Exists:** `POST /v1/diagnostics/calibrate` — DB, embeddings, gateway, retrieval, storage. `app/ev/diagnostics.py`.
- **Gap:** Not voice. No printer/radio checks. Result not HUD/TTS.
- **Technical design:**
  1. Tool `calibrate` `{}` → `run_calibration()`. Speak worst check + status.
  2. Emit `ev.hud.card.v1` with per-check rows.
  3. Extension points: when 30/33/45 exist, append checks (`octoprint.ping`, radio RSSI) inside `run_calibration` — do not fork a second probe.
  4. Persist last report for item 3 self-status.
- **Real-time target:** “Check the calibration.” Speaks worst check + HUD.
- **Wave:** 2. **Fallback:** existing HTTP/CLI `ev checkup`.
- **Depends on:** diagnostics, 1, 10.

#### 27. Analysis, calibrations, diagnostics on the workbench

- **From:** E.V., J.A.R.V.I.S.
- **Exists:** Same calibrate + CLI + ops metrics.
- **Gap:** No live strip on `/app` / lookout; no daemon probe → callout on flip.
- **Technical design:**
  1. Daemon tick: run calibrate (or a cheap subset) on an interval; compare to last; on new `failed` → `emit_callout` (10) unless quiet (9).
  2. Web/Mac: poll `GET /v1/diagnostics/last` and render a strip (reuse card schema).
  3. Do not spam: fingerprint `diagnostics:{name}:{status}`.
- **Real-time target:** Workbench always shows last calibrate; flip to failed → one callout.
- **Wave:** 2. **Fallback:** strip stale with timestamp.
- **Depends on:** 26, 9, 10.

#### 28. HUD cards on phone, watch, or glasses

- **From:** E.V., Karen, E.D.I.T.H.
- **Exists:** `ev.hud.card.v1` / briefing / lookout / route / ops / focus. Watch complication, `ev card`, lookout HTML, Mac helper.
- **Gap:** Cards not default voice-response shape. Glasses renderer missing. iOS widget may be partial.
- **Technical design:**
  1. After tool_loop, if the result is status-like, `hud.validate_hud` + push to lookout (`compose_and_maybe_open`) and Watch/widget endpoints.
  2. One JSON, many renderers. Glasses = new client of the **same** schemas (Wave 5). Do not invent `ev.hud.glasses.*`.
  3. Voice still speaks a one-line summary; card holds the rest.
- **Real-time target:** Wave-2+ status answers appear on Mac lookout, web, Watch, iOS widget.
- **Wave:** 2 + 5 glasses. **Fallback:** CLI `ev card`.
- **Depends on:** schemas, 11.

#### 29. Scientific research help with sources

- **From:** E.V., J.A.R.V.I.S.
- **Exists:** `app/ev/research.py` sessions, notes, citations, optional web.
- **Gap:** Not default voice path. Filter may strip citations.
- **Technical design:**
  1. Tool `research` `{question}` opens/continues a session, calls retrieval + `search_web` when needed, writes notes + conclusion memory.
  2. Response shape `{answer, citations:[{title,url}]}`. Output filter **allowlist** citation URLs (do not strip).
  3. HUD lists sources. Spoken answer includes “according to {n} sources.”
- **Real-time target:** “Research X — cite sources.” Short spoken answer + URL card.
- **Wave:** 2. **Fallback:** memory-only if web disabled; say so.
- **Depends on:** 1, search, 28.

#### 30. Drive a 3D printer / fabricator

- **From:** E.V. fabricator
- **Exists:** `app/ev/maker.py` projects, BOM, `PrintJob` **queue**. No OctoPrint.
- **Gap:** Adapter + progress + voice start.
- **Technical design:**
  1. Adapter `octoprint` (`local` fake job; real = OctoPrint/Moonraker REST, key in vault).
  2. `create_print_job` already queues; `start` calls adapter and stores job id. Status poll → `update_print_job`. Terminal states → callout (10).
  3. Tool `print_start` `{project|gcode}` confirm + Training Wheels + audit.
  4. Consumables (22) from remaining filament if API offers it.
- **Real-time target:** “Print the spacer.” Confirm → start/done/fail callouts.
- **Wave:** 5 printer; queue UX in 2. **Fallback:** queue + “no printer connected.”
- **Depends on:** 20, 22, vault.

#### 31. Suit and desk: same assistant on wearable + workstation

- **From:** E.V., J.A.R.V.I.S.
- **Exists:** Registry, web workbench, Watch card. Conversation not unified (11).
- **Gap:** Wearable is HUD-only; need live transcript both ways.
- **Technical design:**
  1. Same `conversation_id` as 11. Watch can POST a short utterance (`/v1/voice/utterance` or chat) into that thread.
  2. Lookout SSE shows it. Watch shows the reply card (28).
  3. Do not run full TTS on Watch unless complication/audio session allows; prefer haptic + card.
- **Real-time target:** Ask on Watch, see answer on Mac lookout, and vice versa.
- **Wave:** 1–2. **Fallback:** Watch remains card-only if mic denied.
- **Depends on:** 11, 28.

#### 32. CAD / design assist and “how long will this take”

- **From:** J.A.R.V.I.S.
- **Exists:** `PrintJob.estimated_minutes`. No CAD parse.
- **Gap:** STL/STEP/SVG ingest + slicer or heuristic.
- **Technical design:**
  1. Upload attachment → object store. Optional local slicer CLI (PrusaSlicer) in a sandbox (`app/tools/sandbox.py`). Parse time/grams from stdout.
  2. If slicer missing: bounding-box heuristic + **label `estimate=owner_or_heuristic`**.
  3. Tool `estimate_print` `{attachment_id}`. Design questions go to research (29), not a fake CAD kernel.
- **Real-time target:** Drop STL → “About 42 min, 11 g” or honest owner estimate.
- **Wave:** 5. **Fallback:** owner-entered minutes.
- **Depends on:** 30, 29.

#### 33. Monitor *your* vehicle or drone in a test

- **From:** J.A.R.V.I.S. flight-test
- **Exists:** Gear snapshots. No NMEA/MAVLink/OBD.
- **Gap:** Telemetry ingest + HUD + weather overlay via **existing** `get_weather`.
- **Technical design:**
  1. `POST /v1/telemetry/sample` `{source: drone|vehicle|phone, battery, alt, speed, lat, lon}`.
  2. Owner starts `test_session`. Lookout strip binds to latest sample.
  3. Overlay 37: `weather_results` at sample lat/lon (or home). Do **not** add a weather adapter.
  4. Phone-as-sensor fallback: iOS posts GPS/baro labeled `source=phone`.
- **Real-time target:** During a test, “how’s the battery?” speaks last sample.
- **Wave:** 5. **Fallback:** phone stand-in, spoken as such.
- **Depends on:** 37 (exists), 28, 45.

#### 34. Device power / battery / storage

- **From:** J.A.R.V.I.S., Karen
- **Exists:** `gear.scan` Mac probe + snapshots.
- **Gap:** Phones/watch must push battery on heartbeat. Voice query.
- **Technical design:**
  1. Extend heartbeat payload: `{battery_pct, storage_free_b}`. Persist on `Device` or `GearSnapshot`.
  2. Tool `gear_power` `{device?}`. Voice: “What’s the phone battery?”
  3. Low threshold → item 22 callout.
- **Real-time target:** Heartbeat fields + spoken query + low-battery callout.
- **Wave:** 2. **Fallback:** Mac-only scan (today).
- **Depends on:** 11, 22, gear.

#### 35. Wearable vitals: sleep, heart rate, “you took a hit / you look wrecked”

- **From:** J.A.R.V.I.S., Karen, E.V.
- **Exists:** `health_radar.py` — snapshot, readiness, anomalies, morning brief. `POST /v1/health/snapshot`.
- **Gap:** No HealthKit ingest. Brief not spoken.
- **Technical design:**
  1. iOS HealthKit later-wave posts the **same** snapshot schema (do not fork).
  2. Tool `health_how_do_i_look` → `morning_brief` / latest clinical. Speak readiness + flags. **Not** spider DNA.
  3. Daemon at quiet-hours end: `emit_callout` with morning brief if policy allows (9).
- **Real-time target:** “How do I look?” + optional spoken morning brief.
- **Wave:** 2 API/voice, 5 HealthKit. **Fallback:** manual snapshot (already works).
- **Depends on:** 9, 10, health_radar.

#### 36. Concussion / “you hit your head” — symptom check + see a doctor, not a diagnosis

- **From:** Karen
- **Exists:** Nothing dedicated.
- **Gap:** Scripted screening. Must not diagnose.
- **Technical design:**
  1. Deterministic script in `app/ev/health_radar.py` (or `interaction.py`): questions + **fixed** closer “I’m not a doctor. Get medical care if …”
  2. Tool `head_injury_screen` — **no model diagnosis**. Optional snapshot log (`kind=symptom_check`).
  3. Offer item 14 only if owner asks to call someone.
  4. Output filter: strip any model attempt to conclude concussion yes/no.
- **Real-time target:** “I hit my head.” Script + disclaimer. No yes/no diagnosis.
- **Wave:** 2. **Fallback:** disclaimer-only if owner aborts.
- **Depends on:** 1, 8, 14 optional.

#### 37. Weather and local environment from public sensors

- **From:** J.A.R.V.I.S.
- **Exists:** First-class `get_weather` in `app/ev/tools.py` → `weather_results` in `app/search/live.py`. Open-Meteo geocoding + forecast (no API key): current, 3-day, WMO codes. Live search routes `is_weather_query`. Tests: `backend/tests/test_live_search.py`.
- **Gap:** Not voice/HUD default. Items 16 and 33 do not consume it. Optional home lat/lon.
- **Technical design:**
  1. **Do not add a weather adapter.** Wire the existing tool as the voice default for weather intents (tool_select / `needs_live_lookup` already true).
  2. After `get_weather`, emit `ev.hud.card.v1` from snippets + speak the first result.
  3. `route_briefing` (16) and telemetry HUD (33) call `weather_results` with dest/sample coords.
  4. Optional `EV_HOME_LAT/LON` or prefs so bare “what’s the weather?” does not ask for a city.
- **Real-time target:** “What’s the weather?” speaks Open-Meteo + HUD. 16/33 overlay the same function.
- **Wave:** 2. **Fallback:** already ships — omit place → coarse location or ask for city.
- **Depends on:** `get_weather` (exists), 1, 10, 28.

#### 38. Brief a team: “here’s the view, here’s the risk”

- **From:** J.A.R.V.I.S. “view from upstairs”
- **Exists:** `POST /v1/tactical/brief`, `ev.hud.briefing.v1`, lookout ops.
- **Gap:** Not voice-default. Share only via delegate (24).
- **Technical design:**
  1. Tool `brief_me` → existing `tactical.brief`. Speak condensed; HUD full card.
  2. `brief_share` `{delegate}` only if delegate scope `briefing:read`.
  3. Do not invent a second briefing engine.
- **Real-time target:** “Brief me.” Spoken + HUD. Share only if Ned is a delegate.
- **Wave:** 2. **Fallback:** HTTP/CLI already works.
- **Depends on:** tactical, 28, 1.

#### 39. Locate teammates who **opted in**

- **From:** J.A.R.V.I.S., Karen
- **Exists:** `GET /v1/people/{name}/whereabouts` = **memory** last-seen.
- **Gap:** Opt-in live share ingest. Never hunt strangers.
- **Technical design:**
  1. Table `location_shares` (`person_id`, `token_expires`, `last_lat/lon`, `source`). Only created by that person’s consent flow (or owner’s family device they control).
  2. Tool `where_is` `{name}`: if live share valid → map card; else memory whereabouts + “memory only.”
  3. Refuse names with no person entity and no share. No city camera (futuristic).
- **Real-time target:** Opted-in → last ping. Else honest memory line.
- **Wave:** 4. **Fallback:** memory-only (today).
- **Depends on:** 16, people, 24.

#### 40. Replay **your** cameras at **your** place

- **From:** E.V. apartment cam
- **Exists:** `app/ev/vision.py` on uploads. No NVR.
- **Gap:** Owner RTSP/HomeKit adapter.
- **Technical design:**
  1. Adapter `cameras` with owner-only URLs in vault. `clip(start,end)` → blob.
  2. Tool `camera_replay` `{camera, at}`. HUD/web plays blob. Voice summary via `analyze_attachment`.
  3. **Never** discover cameras on the LAN without owner add. No stranger CCTV.
- **Real-time target:** “Show the lab camera from 4pm.” Owner footage + optional summary.
- **Wave:** 5. **Fallback:** owner uploads a clip.
- **Depends on:** 28, vision.

#### 41. Public crime / news / scanner-style alerts you subscribed to

- **From:** E.V. / Polygon
- **Exists:** Alert radar watchlist on **owner** events. Live search. No RSS/NWS public feeds.
- **Gap:** Subscription sources + voice watchlist.
- **Technical design:**
  1. Wave 2: voice `watchlist_add` / `alerts_digest` on existing radar.
  2. Wave 4: adapter `public_feeds` (RSS, NWS alerts). Owner picks feeds. Daemon poll → watchlist match → callout (10).
  3. Optional public audio stream URL the owner pasted — not a private scanner hack.
  4. Quiet hours apply. No city CCTV.
- **Real-time target:** “Subscribe to NWS for my county.” Hits → callouts. Digest on demand.
- **Wave:** 2 + 4. **Fallback:** owner watchlist only.
- **Depends on:** 9, 10, alert_radar.

#### 42. “Is this video likely fake?” at best-effort accuracy

- **From:** E.D.I.T.H. “not an illusion”
- **Exists:** Vision/OCR/scene. No deepfake score.
- **Gap:** Heuristics + **never certainty**.
- **Technical design:**
  1. `analyze_media_authenticity(blob)`: container metadata, encoder tags, known-generator watermarks, optional later model. Output `{label: likely_edited|no_known_artifacts|inconclusive, reasons[]}`.
  2. Tool `media_check` `{attachment_id}`. Spoken sentence **must** include “this is not proof.”
  3. Do not use people face-index here (stranger ID is futuristic).
- **Real-time target:** Share video → likely/inconclusive + reasons. Never “this is real.”
- **Wave:** 4. **Fallback:** metadata-only.
- **Depends on:** vision, 29.

#### 43. Voice changer as a joke / accessibility filter

- **From:** Karen interrogation — **audio FX only**
- **Exists:** Kokoro/hosted TTS voices.
- **Gap:** Picker + rate. Ban “interrogation mode” copy.
- **Technical design:**
  1. `AssistantProfile.tts_voice_id`, `tts_rate`. Tool `set_voice` `{voice_id|lower|slower}`.
  2. TTS provider already takes a voice name — pass it through (`app/voice/tts.py`).
  3. UI/tool descriptions: “voice” / “accessibility.” Filter rejects “interrogation,” “intimidate.”
- **Real-time target:** “Use the lower voice.” Subsequent TTS changes until reset.
- **Wave:** 4. **Fallback:** default voice.
- **Depends on:** 1, 2.

#### 44. Look up **public** records where the law allows

- **From:** Karen dossier — **public only**
- **Exists:** Web search + output filter.
- **Gap:** Allowlisted sources + dox refusal.
- **Technical design:**
  1. Adapter or search wrapper `public_records` allowlist: Wikipedia, SEC EDGAR, official gazettes, company registries. No people-search brokers.
  2. Tool `public_lookup` `{query, kind: org|law|filing}`. Output filter already has PII policy — tighten: refuse phone/home of a private person.
  3. Always cite URLs (29).
- **Real-time target:** “What’s public about {company}?” cited. Private number → refuse.
- **Wave:** 4. **Fallback:** Wikipedia/search with refusal.
- **Depends on:** 29, search, filter.

#### 45. A small recon drone **you own**, on a leash

- **From:** Karen “Droney”
- **Exists:** Fleet tasks conceptually. No SDK.
- **Gap:** Owner-paired drone, geofence, confirm, no weapons.
- **Technical design:**
  1. Adapter `drone` (`local` sim; real Tello/MAVLink). Pairing stores id in vault.
  2. Commands: `takeoff`, `hover`, `land`, `rtl`. **No** weapons verbs — parser refuse.
  3. Geofence = owner home polygon or LOS radius. Confirm takeoff. Training Wheels (19) locked until configured.
  4. Optional JPEG to lookout. Telemetry via 33.
  5. Audit every cmd.
- **Real-time target:** “Take off and hover.” / “Land.” Sim in CI.
- **Wave:** 5. **Fallback:** simulated drone labeled as sim.
- **Depends on:** 21, 33, 19.

#### 46. Track a **beacon you planted on your own gear**

- **From:** Karen tracer — **own gear only**
- **Exists:** Device registry, not AirTags.
- **Gap:** Owner-registered beacons.
- **Technical design:**
  1. Table `beacons` (`label`, `kind: findmy|ble|ev_device`, `last_lat/lon`, `owner_only=true`).
  2. Ingest: Find My export / BLE client / last-seen of registered EV devices.
  3. Tool `find_gear` `{label}`. Refuse if label matches a **person** without a beacon row.
- **Real-time target:** “Where’s my backpack tag?” map card.
- **Wave:** 5. **Fallback:** last-seen of EV devices only.
- **Depends on:** 16, 11.

#### 47. Rough structure estimates from photos, maps, or known plans

- **From:** Karen ferry X-ray **idea** — not live FEA
- **Exists:** Vision on attachments.
- **Gap:** Measure tool with disclaimer.
- **Technical design:**
  1. Tool `estimate_structure` `{attachment_id, reference_length?}`. Use vision labels + optional EXIF/focal length. Output `{guess, unit, confidence, disclaimer}`.
  2. Spoken: always “low confidence” / “if the bottle is 20 cm.”
  3. **Never** “X-ray complete” or “98% of load paths.”
- **Real-time target:** Photo of a shelf → rough width + disclaimer.
- **Wave:** 4. **Fallback:** ask for a reference object.
- **Depends on:** 29, vision, 32 optional.

#### 48. Quiet “something’s off” alerts from **your** patterns

- **From:** E.V. spider-sense analog
- **Exists:** EV Sense, attention policy, health anomalies, calendar signals, isolation scan.
- **Gap:** One fused pass + spoken “why now?”
- **Technical design:**
  1. Daemon: `generate_predictions` + health anomalies + calendar proximity + isolation → **merge** to ≤1 candidate.
  2. `apply_attention_policy` / `may_speak_proactive`. If allowed: one callout with `why_now` citing source ids.
  3. Tool `why_did_you_ping` reads last sense callout.
  4. Dismiss = existing `dismiss_alert`.
- **Real-time target:** One quiet card, not a barrage. “Why did you ping me?” cites sources.
- **Wave:** 2. **Fallback:** predictions stored, not spoken, if policy denies.
- **Depends on:** 9, 10, 15, 35, 41, ev_sense.

#### 49. Company / calendar / inbox help

- **From:** J.A.R.V.I.S. — **assistant, not CFO**
- **Exists:** Calendar read, GitHub adapter, LifeHelper mail.list/send, search.
- **Gap:** Unified plate + draft-not-send.
- **Technical design:**
  1. Tool `whats_on_my_plate` aggregates: calendar upcoming, mail.list (helper), GitHub issues/PRs the token can read, watchlist deadlines.
  2. Tool `draft_reply` `{mail_id}` → draft in DB. `mail.send` only after owner confirm + helper `sent==true`.
  3. Quiet hours: plate is on-demand (not proactive) so it may speak; overnight digest still gated.
  4. No autonomous company writes (no silent GitHub merge, no silent mail).
- **Real-time target:** “What’s on my plate?” “Draft a reply to X” then approve send.
- **Wave:** 3. **Fallback:** calendar-only plate if mail helper missing.
- **Depends on:** 18, LifeHelper mail, GitHub, 9.

---

## Cross-cutting sequences

**First-run:** pair device (12) → Training Wheels (19) → dedication (7) → protocol sheet (4/5) → welcome (6).

**Proactive:** source → `emit_callout` → `may_speak_proactive` (9) → TTS + HUD (10/28) or digest later.

**Actuator:** intent → FeatureGate (19) → confirm if hardware → adapter evidence → audit → speak truth.

---

## Implementation notes (later)

Tests must call the real tool dispatcher or HTTP handler. `local` doubles for HA/OctoPrint/drone/maps. Item 37 tests already exist (`test_live_search.py`) — later work is voice/HUD wiring only.

Parent PR map P0–P8 in `BUILDABLE_FEATURES_PLAN.md` still applies. This file does not change waves except restating **37 ∈ Wave 2 / P2**, not a new adapter.
