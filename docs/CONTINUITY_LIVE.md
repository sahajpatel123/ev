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
- `POST /v1/live/rebuild` — deterministic replay of the live stream into the
  derived layer (resets and marks `consumed`)

### Use

- `GET /v1/state` includes `live_context` (recent live snippets).
- EV Sense and the ops center can consume live signals (e.g., late-night screen
  activity feeds the isolation guardrail; health-belt events feed readiness).
- The chat context compiler includes a separate, privacy-filtered
  `LIVE CONTEXT` section; `never_send_to_model` channels/events never reach
  the model-facing slice.
- `consumed` flag marks events already folded into derived state; rebuild
  resets it, replays the stream, and re-marks folded events.

## 3. E.D.I.T.H. layer (see `EDITH_RESEARCH.md`)

- Focus designation (`/v1/focus`) → chat state + `ev.hud.focus.v1`.
- Fleet (`/v1/fleet`, `/v1/fleet/tasks`) → device presence + task dispatch.
- Ops center (`/v1/ops/center`) → the "global network" dashboard.
- Recognition log (`/v1/vision/annotate`, `/v1/vision/log`) → user-tagged
  identification over user-owned data.
- Digital twin (`/v1/twin`) → aggregate user model.

## 4. Invariants

- Raw events and live events are immutable (tombstone/append-only).
- Every live event carries explicit collector provenance and its effective
  privacy level; ingestion never widens a channel's granted permission.
- `never_send_to_model` content is excluded at retrieval/context assembly.
- Resetting a conversation never deletes history.
- One default thread always exists; `conversation_id` in chat responses is stable.
