# Continuous Conversation & Live Data — Architecture

**Implemented 2026-08-09.** This supersedes the "new chat" mental model: the user
talks to EV in **one lifelong interaction window**.

## 1. Single conversation window

### Database

- `conversation_threads` — one row per thread; exactly one `is_default=True`
  row exists (created on first use). All chat messages use this id.
- `conversation_states` — ephemeral per-thread state: `focus`, `recent_topics`,
  `pending_questions`, `working_context`, `updated_at`. Expires/clears on
  reset; long-term memory is never deleted.

### Behavior

- `POST /v1/chat` without `conversation_id` resolves to the default thread; the
  response always returns that same `conversation_id`. There is no "Chat #1,
  Chat #2".
- Every user/assistant message is an immutable `events` row with
  `conversation_id` set, so the thread is fully auditable and exportable.
- The prompt includes a **ROLLING SUMMARY** (durable, token-bounded, rebuilt from
  events) plus **CONVERSATION HISTORY (continuous window)** — the last 10 turns
  by default — so "continue" works without the user restating context and the
  long arc is never lost to the window.
- `conversation_rollups` stores one compact summary per thread: topics,
  decisions/choices mentioned, open questions, and a recent-turn arc. It is
  derived state and regenerates from raw events (also on tombstone).
- **Progressive depth:** `POST /v1/chat` accepts `context_depth`
  (`auto` | `standard` | `deep` | `deepest`). `auto` promotes continuation
  phrasings ("continue", "where were we?", "pick up where I left off") to
  `deep`, which widens the history window, raises the retrieval cap, adds a
  second retrieval pass over the active task/focus, and raises the token budget.
  Standard context stays bounded (~20k tokens) so the entire lifetime is never
  loaded into every prompt.
- `GET /v1/conversation` returns the thread, ordered messages, current state,
  the rolling summary (`rollup`), and suggested next actions.
- `POST /v1/conversation/reset` clears working state **in the same thread**; the
  message history remains. A `conversation.reset` event is appended for audit.
- `POST /v1/continue` returns the default thread id, the rolling summary, recent
  thread context (plus recent captures), and next actions — a zero-context
  restart for clients resuming mid-thought.

## 2. Live data recording

The user runs the collectors; EV provides the ingestion, storage, and use.

### Database

- `live_channels` — named permissioned sources (`screen`, `audio`, `health`,
  `app`, `vision`, `location`) with a privacy level and metadata.
- `live_events` — immutable units of live data: `channel_id`, `occurred_at`,
  `ingested_at`, `event_type`, `payload`, `device_id`, `collector`,
  `privacy_level`, `sha256`, `consumed`. Derived state rebuilds from these;
  nothing is edited. A unique `(channel_id, sha256)` constraint makes replays
  idempotent, and event privacy is fail-closed (never less restrictive than
  the channel's granted permission).
- `live_derived_state` — per-channel derived rollups (counts, first/last
  event, signal flags with basis live-event ids). Dropped and replayed
  deterministically by `POST /v1/live/rebuild`; never edited in place.

### API

- `POST /v1/live/channels`, `GET /v1/live/channels`
- `POST /v1/live/channels/{id}/events` — single-channel batch
- `POST /v1/live/events` — cross-channel batch (auto-creates channel)
- `GET /v1/live/channels/{id}/events`, `GET /v1/live/status`
- `GET /v1/live/stream` — SSE tail of live events with `access=user|model`
  privacy slices; `since=` replays the window before tailing
- `POST /v1/live/rebuild` — deterministic replay of the live stream into the
  derived layer (resets and marks `consumed`)
- `POST /v1/live/retention` — applies the configured retention window
  (dry-run by default); only consumed events past the window are removed, and
  the latest event per channel plus provenance-linked events are always kept

### Use

- `GET /v1/state` includes `live_context` (recent live snippets).
- EV Sense and the ops center can consume live signals (e.g., late-night screen
  activity feeds the isolation guardrail; health-belt events feed readiness).
- The chat context compiler includes a separate, privacy-filtered
  `LIVE CONTEXT` section; `never_send_to_model` channels/events never reach
  the model-facing slice.
- `consumed` flag marks events already folded into derived state; rebuild
  resets it, replays the stream, and re-marks folded events.
- Retention only touches consumed events past the window (default 90 days,
  `EV_LIVE_EVENT_RETENTION_DAYS`); per-channel derived rollups are recomputed
  from the retained stream so replayability stays deterministic.

## 3. E.D.I.T.H. layer (see `EDITH_RESEARCH.md`)

- Focus designation (`/v1/focus`) → chat state + `ev.hud.focus.v1`.
- Fleet (`/v1/fleet`, `/v1/fleet/tasks`) → device presence + task dispatch.
- Ops center (`/v1/ops/center`) → the "global network" dashboard.
- Recognition log (`/v1/vision/annotate`, `/v1/vision/log`) → user-tagged
  identification over user-owned data.
- Digital twin (`/v1/twin`) → aggregate user model.

## 4. Cross-turn referent (the live offer and the turn ledger)

**Implemented 2026-09-13.** The Muse prompt is built as `[system, user]` with no
chat history — one turn per request. Continuity therefore cannot come from a
message list; it comes from two things on the durable cognitive session
(`storage/cognitive/session.json`, shared by the `:8000` API and the `:18000`
voice edge):

- **Turn ledger.** `intent.remember_exchange` appends each owner/Evie exchange
  (bounded to 6, 1200 chars each). `context.compile_context` renders it as
  `RECENT EXCHANGES`, so *every* surface — voice, Mac, iPhone, PWA, text — gets
  the referent for "yes", "that one", "read it".
- **Pending offer.** When Evie's spoken line asks the owner something,
  `intent.set_pending_offer` records it together with the tool that produced it
  (`action`), whether it was an offer to read an artifact aloud (`readout`), and
  a 600 s expiry. `kernel._record_turn` arms it after *every* result kind, not
  just `muse`, and does so for every channel, including phone/device turns that
  return before the model call.

Binding rules:

- `ev.continuity.is_affirmative_reply` / `is_negative_reply` recognise a short
  reply, including a tailed one ("yes, please", "yes, read it out"). An
  utterance that merely *starts* with yes is a new request ("yes the mail from
  Rahul was long"), not a reply.
- A read-aloud confirmation is a **distinct speech act** from approving a parked
  send, and the two grammars are deliberately separate: `"ok read the mail"` can
  answer Evie's offer but can never approve a queued message. Laughter never
  approves anything.
- The newest question wins. `kernel._offer_outranks_parked_send` keeps a parked
  WhatsApp send from swallowing the answer to a question Evie has asked since.
- A greeting does not displace an unanswered offer: the Mac client sends a
  synthetic `Hi.` on every live open (`intent.is_substantive_turn`).
- `intent.continuation_readout` promotes a bare affirmative against a read-aloud
  offer to a readout, so "yes" speaks the body instead of repeating the gist.
- `intent.readout_offer_live` keeps that read-aloud intent when the model calls
  the mail/message tool with no query at all.
- A "which one?" offer carries its numbered `options`, so the owner can answer
  with a slot ("the second one"); `ev.continuity.choice_index` reads the slot
  and an ordinal only answers an offer that actually has options.
- A turn that did not finish what the owner asked for — budget exhausted
  (`in_flight`), provider down (`unavailable`), or a crash (`failed`) — does NOT
  spend his answer; the offer stays live so he can say "yes" again.
- A raise inside a turn is spoken, not swallowed into silence: `handle_turn`
  returns an honest line and still records the exchange.

`storage/cognitive/session.json` is a private store (0700/0600), and the ledger
and offer are credential-redacted on write, because they are re-injected into
later prompts and outlive the input filter that would have scrubbed them.

**Known limitation.** `save()` is last-writer-wins for every field except the
turn ledger, which is unioned from disk when another process wrote first. One
global session is shared by the `:8000` API and the `:18000` voice edge, so a
turn holding its snapshot across a long tool call can still overwrite a
concurrently armed offer with its own view. See the residual section of the
workspace analysis.

`make eval` carries a `continuity` gate that asserts all of the above without a
live model, so this class of regression cannot land silently.

## 5. Invariants

- Raw events and live events are immutable (tombstone/append-only).
- Every live event carries explicit collector provenance and its effective
  privacy level; ingestion never widens a channel's granted permission.
- `never_send_to_model` content is excluded at retrieval/context assembly.
- Resetting a conversation never deletes history.
- One default thread always exists; `conversation_id` in chat responses is stable.
