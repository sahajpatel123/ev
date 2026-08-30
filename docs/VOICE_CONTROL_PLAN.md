# Voice Control & Memory Retrieval — EV Foundation Plan

**Status:** Implemented (2026) — see "Implementation map" below for exact wiring.
**Applies to:** GPT Realtime 2.1 mini (`EV_VOICE_LIVE_BRAIN=openai`) and Grok Voice via
`backend/app/voice/live/grok_voice.py`.
**Goal:** Control the entire laptop (any app, any background task) with a small fixed set of
UI verbs — never a per-app tool treadmill — and fetch past history fast as chunked evidence,
clearly separated from future event/reminder retrieval.

---

## 1. TL;DR

The previous live surface exposed ~48 supervised tools; every new action required a new tool
and the model had to guess among them. This plan replaces that treadmill with:

1. **`recall_history`** — a first-class **past/history retrieval** tool (typed memories,
   time ranges, `as_of` time travel, brief/full chunks, cursor pagination). Deliberately
   separate from event/reminder retrieval (`get_upcoming_alerts`, `calendar_read`,
   `set_reminder`, `search_timeline`), because past scoring (semantic weights + version
   windows) is a different engine from future scoring (deadline + priority).
2. **10 UI verbs** (`read`, `see`, `click`, `double_click`, `right_click`, `type`, `paste`,
   `key`, `scroll`, `drag`) — UI-specific primitives that work on *any app*, forever. New
   actions are a new `query` string to `read`, never a new tool name.
3. **Shadow memory injection** (`EV_VOICE_LIVE_MODE=shadow`) — history chunks are injected
   into the session/response instructions read-only, so the speech-to-speech model answers
   past questions with **zero function calls**; `recall_history` remains as the explicit
   fallback when the shadow block is empty.
4. **`EV_VOICE_LIVE_MODE=autonomous`** — zero tools, pure speech-to-speech chat (demo /
   casual; no memory, no provenance — deliberately not the default).
5. Default mode **`supervised` is byte-identical to today** — every change is additive and
   gated; nothing existing is removed or re-tuned.

## 2. The two retrievals must stay separate

| | Past / History (`recall_history`) | Event / Reminder (existing) |
|---|---|---|
| **Index** | `memories` (typed, versioned, embedded, `valid_from/until`, `embedding_model_version`) | `events`, `alerts`, `calendar`, `routines` (deadline/priority) |
| **Scoring** | locked hybrid 0.35 semantic + 0.20 keyword + 0.15 recency + 0.15 importance + 0.10 relationship + 0.05 confidence + per-query calibration | deadline proximity, priority, quiet hours, dedup |
| **Time** | backward (as-of / version windows) | forward (next 7 days, leave-by) |
| **Chunk** | semantic chunks, `brief|full`, cursor-paginated | one alert/event per record |
| **Permission** | `memory:read`, `never_send_to_model` excluded at SQL | `alerts:read`/`calendar:read`/`life:reminder` |

Mixing them in one tool forced the model to guess which scoring applies. Splitting removes
the guess.

## 3. Supervised commands — the problem

Current: 48 top-level tools in `LIVE_VOICE_TOOLS`
(`backend/app/ev/tool_select.py`), each a `{name, parameters, permission, handler}` block
in `TOOL_SPECS` (`backend/app/ev/tools.py`).

Problems:
1. **Explosion** — every new verb = new spec + handler + test; `session.update` grows;
   model confusion grows with the tool count.
2. **App-specificity** — `app_action` semantics (`play_playlist_track`) apply only to
   supported apps; the long tail is uncovered until someone builds it.
3. **Churn** — features block on building their tool.

**The F4 surface mechanism already exists** (`EV_MODEL_SURFACE_V2`,
`app/ev/capabilities.py:model_surface_mode`, `F4_TARGET_SURFACE`) alongside the F4 tools
`recall` + `computer`. This plan extends the *registry* (additive), not the policy engine.

## 4. UI-specific verbs (build once, control everything)

**Rule that prevents re-explosion:** if a new action needs a new tool *name*, it is wrong.
If it can be done with a new `query` string to `read`, it is right.

| Verb | Maps to (existing primitive) | What it controls (any app) |
|---|---|---|
| `read` | `inspect_ui {query, level}` | Accessibility tree → `e12_1: "Bluetooth [off]"` |
| `see` | `screen_look {target}` | Screenshot when AX is blind |
| `click` | `ui_action {action: press\|click\|click_at}` | Any ref from `read`, or normalized coords + frame |
| `double_click` | `ui_action click` ×2 (sequential, never blind retry) | Open file/folder |
| `right_click` | `ui_action {action: menu}` | Context menus |
| `type` | `ui_action {action: type\|append\|replace}` | Any focused / ref'd field |
| `paste` | `ui_action {action: paste}` | Clipboard text into field |
| `key` | `ui_action {action: keyboard}` | `cmd+space`, `cmd+s`, `enter`, `esc` … opens/operates anything |
| `scroll` | `ui_action {action: scroll}` | Up/down/left/right in any window |
| `drag` | `select` + `click_at` (requires a fresh `frame_id`; honest refusal otherwise) | Sliders, drag-and-drop |

Examples — same verbs, different `query`:
- Trash: `read "Empty Trash"` → `click e4_1`
- Bluetooth: `read "Bluetooth"` → `click e8_2`
- Spotify: `read "lofi"` → `click e7_2`
- Any future app: `read "<its button>"` → `click`

**What stays supervised (by choice, not need):** API-critical actions that are more reliable
than UI clicking and carry real delivery evidence — `send_message`, `place_call`,
`list_messages`, `list_mail`, `resolve_contact`, `calendar_add`, `calendar_read`,
`set_reminder`, `start_timer`, `open_url`, `search_web`, `get_weather`, `look`,
`brief_me`, `calculate`, `present`, `evie_turn` + `life_*` canonical state. These are
generic capabilities — not per-app verbs — so they do not re-open the treadmill.

## 5. Shadow mode (history without function calls)

`EV_VOICE_LIVE_MODE=shadow` (additive; default remains `supervised`):

- The realtime session advertises **only** the curated `SHADOW_VOICE_TOOLS` surface
  (UI verbs + `recall_history` + the generic capabilities above). Raw per-app computer
  names (`inspect_ui`, `ui_action`, `screen_look`, `app_action`) and generic memory tools
  (`search_memory`, `search_decisions`, `search_timeline`) are **not advertised** — the
  verbs replace them.
- Every completed owner transcript triggers a **shadow recall** (`build_shadow_memory`:
  `Retriever.search(access="model")`, brief chunks, token budget) and injects
  `SHADOW MEMORY: …` into the session instructions (session.update) / the next
  `response.create` instructions. The model speaks directly about the past — **no tool
  call, no second turn, ~0 extra latency budget**.
- `recall_history` remains advertised as the explicit fallback ("go deeper / what about…")
  and for the typed-chat surface.
- Shadow is read-only: it never creates prompts outside the existing privacy boundary
  (`never_send_to_model` excluded at SQL; `sensitive` excluded unless opted in).

**Why this beats pure autonomous:** pure autonomous has zero memory (hallucinated life
facts, no provenance). Shadow keeps the instant speech path but grounds it in real stored
chunks that carry `event_id` provenance (auditable via `GET /v1/audit/{id}`).

## 6. Autonomous mode (complete handover, no memory)

`EV_VOICE_LIVE_MODE=autonomous` → `tools=[]`, `tool_choice=none`, identity instructions only.
Fastest end-to-end latency; deliberately **not** for personal recall or actions. Use for
demo/casual chat; switch back for daily use.

## 7. Implementation map (additive-only — nothing existing changed)

| # | Change | File |
|---|---|---|
| 1 | Settings: `voice_live_mode`, `voice_shadow_k`, `voice_shadow_budget_tokens`, `voice_shadow_min_score`, `ui_verb_tools_enabled` | `backend/app/config.py` (appended to Settings class) |
| 2 | New module: `recall_history` + cursor + shadow builder | `backend/app/memory/history.py` (new) |
| 3 | Tool specs: `recall_history` + 10 UI verbs (registry, permissions, risk) | `backend/app/ev/tools.py` (appended to `TOOL_SPECS` + `_handle`) |
| 4 | Surfaces: `LIVE_VOICE_TOOLS` += new names; new `SHADOW_VOICE_TOOLS`; past-tense routing → `recall_history` | `backend/app/ev/tool_select.py` |
| 5 | Spoken labels for new tools | `backend/app/ev/protocols.py` (`_SPOKEN_CAPABILITY_LABELS`) |
| 6 | Mode-aware realtime surface + shadow injection hooks | `backend/app/voice/live/grok_voice.py` (additive, default path untouched) |
| 7 | Env reference + quickstart | `.env.example`, `docs/ENVIRONMENT.md` (appended blocks) |
| 8 | Tests (offline-safe) | `backend/tests/test_history_recall.py`, `test_ui_verbs.py`, `test_shadow_mode.py` (new) |

**Law compliance:** no REST endpoint added/removed → locked `backend/eval/contract_v1.json`
untouched. No schema/migration change. No shared-file line modified — only appended. The
frozen live-voice default path stays byte-identical while `voice_live_mode=supervised`.

## 8. Foundation rule for future work

Any future plan must pass: *"Can it be done with the same fixed UI verbs + a new `query`
string?"* If yes → no build. If no → it is a new app verb and should be rejected unless it
is API-critical (real delivery evidence) — in which case it is a generic capability, not a
per-app verb.