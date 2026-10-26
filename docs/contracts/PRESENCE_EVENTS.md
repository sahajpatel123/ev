# PRESENCE EVENTS — backend → app event stream contract

Contract: **`evie-presence/1`** (version 1, additive-only).
Status: **proposed by the presence session — server side provided by CLI 1.**
Companion recon: `docs/PRESENCE_RECON.md` §§2–4.

This contract is the only wire between Evie's brain loop and the native
macOS face. Everything the HUD pill (D3), confirm card (D4), and rewind
timeline (D5) display arrives as one of these events, or as a normal
authenticated REST call the event points at. Nothing here invents a second
approval authority: approve/deny/undo/cancel/panic are the existing REST
routes from recon §2; the stream only *announces* them.

## 1. Transport

`GET /v1/fabric/presence/stream` — **Server-Sent Events** over the existing
authenticated local channel. **(Provided by CLI 1.)**

```http
GET /v1/fabric/presence/stream?cursor=<opaque>&device_id=<registry-uuid> HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <device token, same as all EVAPIClient calls>
Accept: text/event-stream
Last-Event-ID: <last seen envelope id, optional>
```

Why SSE GET and not the alternatives (recon §4):

- The app already parses SSE (`EVClient/Streaming.swift`) and already holds
  long-lived connections (`liveSocketSession`: waits-for-connectivity, long
  resource timeout). No new dependency, no new auth, no new permission.
- A `GET` stream is **safe to auto-reconnect**. The existing POST-turn
  streams (`/v1/chat`, `/v1/voice/utterance/stream`) forbid silent reconnect
  precisely because a retry would re-execute the turn; a read-only GET has
  no such hazard.
- Presence needs server→client only. App→backend traffic (approve / deny /
  undo / cancel / reverify) stays plain REST RPC. A WebSocket would add
  duplex machinery (ping/pong, generation guards — see `LiveVoice.swift`)
  for a duplex need that does not exist.
- `Last-Event-ID` gives replay without a second endpoint. A polling fallback
  (`GET /v1/fabric/presence/since?cursor=`, same envelopes as JSON array —
  **provided by CLI 1**) covers networks that buffer SSE.

Server behavior:

- `Content-Type: text/event-stream`, `Cache-Control: no-cache`, one `id:` +
  `event:` + `data:` frame per envelope. `:heartbeat` comments at least
  every 15 s so idle connections stay provably alive.
- First frame after (re)connect is always `stream.hello` (§3) carrying the
  server's current cursor, contract version, and server time.
- Auth failure is HTTP 401 with a JSON error body (never an event); the
  client surfaces "backend auth failed", never fake stream data.
- Backpressure: the server may coalesce rapid `turn.transcript.partial` /
  `turn.progress` frames but MUST NOT drop terminal frames
  (`turn.completed`, `turn.failed`, `turn.needs_confirm`, `panic.state`).

### Audio levels (local only — CLI 3 orb note)

The animated orb (CLI 3, branch `evie-orb`) is audio-reactive at animation
rate (~30 Hz). The input (mic) and output (TTS playback) levels that drive
it are published **locally by the app** (`PresenceLevels`: mic tap + TTS
meter on-device) and are **never sent over this event stream** — no
per-frame audio telemetry crosses the wire in either direction. The
`turn.listening.level` field below stays as a coarse activity hint for
state display only; it is not orb drive data, and presence renders no
listening animation of its own (static state + an orb slot; all motion is
CLI 3's). Presence content views (plan steps, captions, confirm card,
status) are plain SwiftUI with no panel dependencies, so they host
unchanged inside `IslandHost` or the floating pill panel.

## 2. Envelope

Every frame's `data:` is one JSON object:

```json
{
  "v": "evie-presence/1",
  "id": "pe-01J9Z…",
  "seq": 41207,
  "ts": "2026-10-26T19:02:11.410Z",
  "turn_id": "ft-a1b2c3d4e5f6",
  "type": "tool.finished",
  "data": {}
}
```

| Field | Type | Rule |
| --- | --- | --- |
| `v` | string | Contract id. Clients ignore frames whose major version they do not know (`evie-presence/2…`) but stay connected. |
| `id` | string | Opaque, unique per frame, stable across replays. Also the SSE `id:` field and the resume cursor unit. |
| `seq` | integer | Global, strictly increasing per server process. Gaps are legal (coalesced frames); going backwards means "resync, your cursor is from another process". |
| `ts` | string | RFC 3339 UTC, server clock. Display uses it; ordering uses `seq`. |
| `turn_id` | string \| null | Fabric turn id (`ft-…`) for turn-scoped events; null for `background.update`, `panic.state`, `stream.hello`. |
| `type` | string | Event name below. Unknown names MUST be ignored (additive rule), never fatal. |
| `data` | object | Per-event schema below. Unknown keys inside `data` MUST be ignored. |

### Ordering guarantees

1. Per turn, frames are emitted in causal order: `turn.started` first, then
   exactly one terminal frame (`turn.completed` | `turn.failed`), and
   `turn.needs_confirm` always precedes the tool/turn frame it gates.
2. `plan.updated` carries the full step list every time (state, not delta);
   `tool.started`/`tool.finished` pair by `call_id`.
3. Global `seq` is monotonic within a process; after a server restart `seq`
   may reset and the client must treat `stream.hello.restarted=true` as
   "drop dedupe windows, re-fetch REST state".
4. Delivery is **at-least-once**: replays may duplicate; every consumer
   dedupes by `id` (recommended window: last 500 ids).

### Reconnection / replay

1. The server keeps a bounded ring of the last **≥1000 frames or ≥30 min**
   (whichever is larger) per device, keyed by `id`.
2. On connect with `Last-Event-ID` (or `?cursor=`), the server replays every
   retained frame after that id, oldest first, then continues live. If the
   cursor is unknown or evicted, the server sends `stream.hello` with
   `resumed=false` and `gap=true`, and the client re-fetches REST state
   (pending approvals, panic state, undo list) before rendering live frames.
3. Client reconnect uses exponential backoff (1s → 30s max, jitter) and
   surfaces "reconnecting" on the HUD after 5 s without frames or heartbeat.
4. Terminal frames are retained until acknowledged by time (30 min), never
   coalesced away: a client that was offline for 10 min MUST still see the
   `turn.needs_confirm` and `turn.completed` frames it missed.

### Versioning

- `evie-presence/1`: this document. Additive-only forever: new event types,
  new optional `data` keys, new `stream.hello` capabilities.
- A breaking change ships as `evie-presence/2` on a new path
  (`/v1/fabric/presence/v2/stream`), with v1 served in parallel for at
  least one release. The client pins the highest `v` it implements.
- `stream.hello.capabilities` advertises optional server features
  (e.g. `rich_confirm`, `receipt_feed`) so the app degrades honestly when
  CLI 1 has not built them yet.

## 3. Events

Common conventions: human-readable `label` fields are pre-composed by the
server (the app never invents wording for actions); `risk_class` is one of
`R0 R1 R2 R3 R4 forbidden`; durations in ms; money never appears (money
actions are DENY, recon §2).

### `stream.hello` (transport; first frame on every connect)

```json
{
  "contract": "evie-presence/1",
  "server_time": "2026-10-26T19:02:11.410Z",
  "cursor": "pe-01J9Z…",
  "resumed": true,
  "gap": false,
  "restarted": false,
  "capabilities": ["rich_confirm", "receipt_feed", "poll_fallback"]
}
```

### `turn.started`

A new owner-visible turn began (voice, hotkey, gesture, typed, scheduled).

```json
{
  "source": "gesture|hotkey|voice|typed|schedule|background",
  "utterance_preview": "Text Sam …",
  "label": "Listening…"
}
```

`utterance_preview` is the first ≤120 chars; the full transcript arrives via
`turn.transcript.final`. Empty string when there is no utterance yet.

### `turn.listening`

Mic is open and streaming audio for this turn.

```json
{ "level": 0.42, "degraded": false }
```

`level` 0..1, a coarse activity hint for state display. `degraded=true`
means the hint is synthetic (no audio tap). Presence renders a static
listening glyph either way — never a fake meter; animation-rate levels for
the orb are local-only (see "Audio levels" above).

### `turn.transcript.partial` / `turn.transcript.final`

```json
// partial
{ "text": "text Sam that I'll be", "sequence": 7, "stable": false, "confidence": 0.61 }
// final
{ "text": "Text Sam that I'll be late.", "confidence": 0.93, "provider": "live-voice" }
```

Partials are ephemeral (superseded, never stored); the final is the turn's
recorded utterance. `confidence` 0..1, honest (0 when unknown, never a
placeholder like 0.99).

### `turn.thinking`

The brain is reasoning; no tool running yet. Carries the first honest status
line so the HUD never sits blank.

```json
{ "label": "Thinking…", "elapsed_ms": 850 }
```

### `plan.updated` (full state, every time)

```json
{
  "label": "Texting Sam",
  "steps": [
    {"id": "s1", "label": "Find Sam's number", "status": "done"},
    {"id": "s2", "label": "Draft the message", "status": "done"},
    {"id": "s3", "label": "Confirm with you", "status": "active"},
    {"id": "s4", "label": "Send", "status": "queued"}
  ]
}
```

`status`: `queued | active | done | failed | skipped`. Steps may be added,
reordered, or re-labeled between frames; the app replaces the whole list.

### `tool.started` / `tool.finished`

```json
// started
{ "call_id": "c-9f2", "step_id": "s2", "name": "comms_act",
  "label": "Drafting message", "target": "Sam (Messages)" }
// finished
{ "call_id": "c-9f2", "step_id": "s2", "name": "comms_act",
  "label": "Draft ready", "ok": true, "verified": true,
  "evidence": {"draft_id": "d-11"},
  "error": null,
  "receipt_id": "rc-77",
  "undo": {"kind": "none"} }
```

`name` is the lane/tool name, `label` the human line, `target` the acted-on
identity. `ok/verified/evidence/error/undo` mirror the fabric receipt
(`fabric/policy.py:_receipt`); `verified` is true only after a read-back.
`undo.kind="none"` when nothing can be undone — the timeline then shows no
Undo button rather than a dead one.

### `ambient.looking_at` (app-local; the backend MUST NOT send this)

Produced on-device from `NSWorkspace` (no new permission, no backend round
trip). Window titles never leave the Mac; this event exists so the HUD has
one pipeline for "what is Evie attending to".

```json
{ "app": "Mail", "window": "Inbox — 3 unread", "bundle_id": "com.apple.mail" }
```

Any field may be `""` when unknown. Sensitive apps (per the owner's
exclusion list) yield `{"app": "", "window": "", "bundle_id": ""}` — the HUD
shows nothing rather than a redaction marker.

### `turn.needs_confirm` (the confirm card — exactness is the point)

```json
{
  "ticket_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "risk_class": "R3",
  "channel": "message.send",
  "label": "Send via Messages to Sam (+1 555-0100)?",
  "recipient": {"name": "Sam Rivera", "address": "+1 555-0100",
                "address_kind": "phone", "first_contact": false},
  "text": "Running 10 min late — start without me?",
  "diff": null,
  "attachments": [],
  "undo": {"kind": "none", "label": "Sending cannot be undone"},
  "expires_at": "2026-10-26T19:04:11Z",
  "approve": {"method": "POST",
              "path": "/v1/fabric/approvals/{ticket_id}/approve",
              "needs_reverify": true},
  "deny": {"method": "POST",
           "path": "/v1/fabric/approvals/{ticket_id}/deny"}
}
```

Rules:

- `recipient` is the **canonical send target** (bound server-side at park
  time, `5f44f73`); `first_contact=true` forces physical confirm and a
  first-contact banner. The app displays `name` + `address` verbatim and
  never truncates `text` (scrolls instead).
- `channel` is one of `message.send | mail.send | call.place | file.write |
  file.delete | sql.dml | sql.ddl | shell.exec | publish | egress |
  credential.use | other`. File/SQL variants put the unified diff or
  statement preview in `diff` (`{"kind": "unified_diff"|"sql_preview",
  "body": "…", "truncated": true|false}`) and leave `text` null.
- `undo.kind`: `none | file_snapshot | trash | action_rollback`, with a
  human `label`. `file_snapshot`/`trash` carry the `GET /v1/fabric/undo`
  entry id when already known.
- `approve.needs_reverify=true` tells the app to mint
  `POST /v1/identity/reverification {purpose: "runtime.action"}` and send
  the token as `X-EV-Reverify` (device-token path; master-key clients omit
  it). The spoken word "yes" MUST NOT trigger the approve call for
  `HIGH_RISK` channels (sends, deletes, SQL DML/DDL, money, credential,
  first-contact) — Return/click on the card only (CLAUDE.md R7).
- Rich payload (`recipient`/`text`/`diff`) is the **`rich_confirm`
  capability — provided by CLI 1**. Until it ships, the server sends
  `label` + `ticket_id` + `risk_class` + `expires_at` only, and the app
  renders the card from `label` with an honest "details unavailable —
  approve in the terminal" note. The app MUST NOT approve a ticket whose
  card it could not fully render; the approve control stays disabled.

### `turn.progress` (spoken-line text)

One line Evie is saying or about to say. Drives live captions.

```json
{ "text": "Found Sam. Drafting your message.", "spoken": true, "sequence": 3 }
```

`spoken=false` for status lines that are shown but not voiced. Ordered by
`sequence` per turn; the app appends, never reorders.

### `turn.completed`

```json
{
  "summary": "Sent to Sam via Messages.",
  "receipt_ids": ["rc-77"],
  "verified": true,
  "label": "Done"
}
```

`verified` is true only if every effecting tool in the turn verified. The
timeline resolves `receipt_ids` through the receipt feed (§4).

### `turn.failed` (honest reason, next step — never a fake success)

```json
{
  "reason": "Sam has no phone number on file.",
  "category": "missing_data|denied|timeout|provider_error|cancelled|other",
  "failed_step_id": "s1",
  "recoverable": true,
  "next_step": "Add Sam's number to Contacts, then ask again.",
  "receipt_ids": []
}
```

`category=cancelled` covers owner/Esc cancels and `POST /v1/fabric/cancel`.
The HUD shows `reason` + `next_step` verbatim.

### `background.update`

Something changed outside the current turn (a goal finished, a parked
ticket expired, a routine fired).

```json
{
  "kind": "goal.done|ticket.expired|routine.fired|sync.note",
  "label": "Evening briefing is ready.",
  "ref": {"type": "briefing", "id": "b-42"}
}
```

`ref` is optional and names the REST object to fetch. No sound, no focus
steal — a menu-bar badge at most.

### `panic.state`

```json
{ "panicked": true, "at": "2026-10-26T19:05:00Z",
  "reason": "Owner pressed the panic hotkey." }
```

Mirrors `GET /v1/fabric/panic`. While `panicked=true` the HUD shows the
panic banner, the gesture engine refuses to start turns, and approve
controls disable. The app never engages or clears panic (master-only).

## 4. REST companions (provided by CLI 1)

The stream announces; these let the app render fully. All additive, Bearer
auth, existing error conventions.

1. **Rich confirm read** (`rich_confirm` capability): either the full
   §`turn.needs_confirm` payload inline (preferred) or
   `GET /v1/fabric/approvals/{ticket_id}` returning the same `data` object
   plus `status` (so the card can show "already decided" instead of dead
   buttons). Redacted server-side (no secrets, no credential material).
2. **Turn receipt feed** (`receipt_feed` capability):
   `GET /v1/fabric/turns?since=&until=&limit=&cursor=` →
   `{turns: [{turn_id, started_at, source, utterance, summary, status,
   verified, steps: [{label, status}], receipts: [{receipt_id, label,
   target, ok, verified, undo_kind, undo_id?}]}], next_cursor}`.
   Powers the rewind timeline (D5). Until it ships, the timeline renders
   from `GET /v1/runtime/actions` + `GET /v1/fabric/undo` + fixtures.
3. **Poll fallback** (`poll_fallback` capability):
   `GET /v1/fabric/presence/since?cursor=&limit=` → `{frames: [envelopes],
   cursor}` with identical replay semantics to §2.

## 5. Worked example (6-step task, abbreviated)

```text
S: event: stream.hello        data: {"contract":"evie-presence/1","resumed":false,…}
S: event: turn.started        data: {"source":"gesture","utterance_preview":"","label":"Listening…"}
S: event: turn.listening      data: {"level":0.5,"degraded":false}
S: event: turn.transcript.final data: {"text":"Text Sam that I'll be late.","confidence":0.93}
S: event: turn.thinking       data: {"label":"Thinking…","elapsed_ms":640}
S: event: plan.updated        data: {"label":"Texting Sam","steps":[…4 steps…]}
S: event: tool.started        data: {"call_id":"c-1","name":"pim_act","label":"Finding Sam",…}
S: event: tool.finished       data: {"call_id":"c-1","ok":true,"verified":true,…}
S: event: turn.progress       data: {"text":"Found Sam. Drafting your message.","spoken":true}
S: event: tool.started/finished (comms_act draft, verified:true)
S: event: plan.updated        (step 3 active: Confirm with you)
S: event: turn.needs_confirm  data: {ticket_id, risk_class R2, recipient, full text, …}
— owner presses Return on the card; app mints reverify, POSTs approve —
S: event: tool.started/finished (comms_act send, verified:true, receipt_id rc-77)
S: event: turn.progress       data: {"text":"Sent.","spoken":true}
S: event: turn.completed      data: {"summary":"Sent to Sam via Messages.",…}
```

## 6. Conformance checklist (for CLI 1's server side)

- [ ] SSE GET with Bearer auth, `stream.hello` first, `:heartbeat` ≤15 s.
- [ ] `Last-Event-ID` / `?cursor=` replay from a ≥1000-frame / ≥30-min ring;
      `gap=true` + `resumed=false` when the cursor is gone.
- [ ] Causal per-turn order; terminal frames never coalesced; at-least-once
      with stable `id`s.
- [ ] `turn.needs_confirm` carries the full §3 payload (or `rich_confirm`
      is absent and the payload is honestly minimal).
- [ ] Spoken "yes" path never approves HIGH_RISK (server ALSO enforces:
      a voice-channel approve of a HIGH_RISK ticket is rejected even if a
      client bug sends it — defense in depth for R7).
- [ ] No secrets, credential material, or full window titles in any frame.
- [ ] Additive-only: this v1 set stays byte-compatible as fields are added.
