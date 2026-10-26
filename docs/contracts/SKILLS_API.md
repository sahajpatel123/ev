# SKILLS_API — contract between HANDS (CLI 4) and the brain's tool layer (CLI 1)

**Version 1.0.0** (semver; additive only within 1.x). Module: `app.fabric.skills.api`.
Receipt contract tag: `evie-hands/1`. Everything here is a **Python interface plus
JSON shapes**; CLI 1 owns the tool names and schemas the model sees and maps them
onto these calls. This package never edits the kernel loop, the context
assembler, tool schemas, the doctrine prompts or `cognitive/capabilities.py`.

Versioning rules: fields may be added to any object, new `skill` names and new
`error.code` values may appear. A field is never renamed, retyped or removed in
1.x. Consumers must ignore unknown fields. `hands_api_version()` returns the
string; `skill.list()` reports it too.

## 0. Calling convention

* The facade is a plain object: `from app.fabric.skills.api import Hands`;
  `hands = Hands()` (stateless apart from caches and the watcher registry).
* Every method is **blocking** (osascript, sockets). From async code call
  `await hands.aio.skill_run(...)` (same names, runs in a worker thread) or
  `asyncio.to_thread`.
* No method raises on an operational failure. It returns a receipt with
  `ok=false` and a stable `error.code`. Programming errors (bad types) raise.
* No method prints, logs a secret, or sends a message to anyone.

## 1. The receipt (R6)

Every effectful or perceiving call returns this object.

```json
{
  "contract": "evie-hands/1",
  "ok": true,
  "verified": true,
  "evidence": {},
  "error": null,
  "undo": null,
  "skill": "web_media.skip_ad",
  "channel": "dom",
  "plan": [],
  "steps": [
    {"n": 1, "phase": "resolve", "op": "world.query", "channel": "applescript", "ok": true, "ms": 41, "evidence": {}, "error": null},
    {"n": 2, "phase": "perceive", "op": "dom.ad_state", "channel": "dom", "ok": true, "ms": 38, "evidence": {"ad": true, "skippable_in_s": 3}},
    {"n": 3, "phase": "wait", "op": "wait_until skip_visible", "ok": true, "ms": 3011},
    {"n": 4, "phase": "act", "op": "dom.click skip", "ok": true, "ms": 22},
    {"n": 5, "phase": "verify", "op": "dom.ad_state", "ok": true, "ms": 61, "evidence": {"ad": false}}
  ],
  "provenance": "owner",
  "needs_confirm": false,
  "confirm": null,
  "say": "Skipped the ad.",
  "learned": null,
  "timings_ms": {"total": 3240, "first_effect": 3050}
}
```

Rules (all binding):

1. `verified=true` **only** after the world was read back and the postcondition
   held (`phase:"verify"` step present and `ok`). An effect with no readable
   postcondition returns `ok=true, verified=false`, and `say` must not claim success.
2. `say` is the one short sentence the voice/text reply may use. It is present only
   when it is true: `"Skipped the ad."` appears only on a verified skip; a failure
   carries an honest sentence (`"The ad can't be skipped; 11 seconds left."`).
   The brain may reword but must not claim more than `say` supports.
3. `error` is `null` or `{"code": "...", "message": "...", "fix": "...|null", "retryable": bool}`.
   Stable codes: `not_running`, `not_found`, `ambiguous`, `permission_missing`
   (with `fix` naming the exact System Settings path or browser setting),
   `js_disabled` (browser "Allow JavaScript from Apple Events" is off),
   `locked` (screen locked), `display_asleep`, `unsupported`, `precondition_failed`,
   `timeout`, `denied` (policy DENY), `confirm_required`, `unverified`,
   `bridge_unavailable`, `stale_ref`, `invalid_args`, `internal`.
4. `undo` is `null` or `{"skill": "...", "args": {...}, "description": "..."}`: a
   `skill.run`-able inverse (close the tab we opened, reopen the closed tab id).
5. `steps[]` use the loop phases `resolve | perceive | act | wait | verify | recover`.
   `channel` is the rung of the ladder that did the work:
   `applescript | cli | url_scheme | dom | ax | keys | pointer | ocr | vision | tmux | fs`.
6. Evidence never contains secrets, cookies, tokens, or the contents of secure
   fields. Screenshot-derived strings are marked `"untrusted": true` inside evidence.

### Confirmation (the one approval authority)

HANDS never keeps its own approval store. Effects go through
`app.fabric.policy` (`decide`, `risk_class`, `physical_confirm_required`). When
policy returns CONFIRM and the call was not `approved=True`, the call performs
**no effect** and returns:

```json
{"ok": false, "verified": false, "needs_confirm": true,
 "error": {"code": "confirm_required", "message": "...", "fix": null, "retryable": true},
 "confirm": {
   "title": "Open a new Ghostty tab with 4 panes",
   "rows": [{"label": "pane 1 (top-left)", "cwd": "~/Code/ev", "command": "codex resume 01a0…", "grounded": "codex session 01a02222… found", "untrusted_source": true}],
   "risk": "confirm",
   "physical": false,
   "plan_id": "plan-9f3a…",
   "expires_s": 300
 }}
```

CLI 1 shows `confirm` on CLI 2's confirm card and re-calls the **same** method with
`approved=True, approval_id=<id from the card>`. If any row classifies into a
`HIGH_RISK` class (`policy.risk_class`) then `confirm.physical=true` and only a
physical confirmation (Return/click on the card, paired phone) may approve; a
spoken "yes" must not. `plan_id` binds the approval to the exact plan: if the
re-call's args produce a different plan the call returns `confirm_required` again.

### Provenance (R7)

Every effectful method takes `provenance="owner"` by default. Pass
`"screenshot" | "web" | "email" | "message" | "ocr" | "audio" | "terminal_output"`
for anything derived from the environment. Untrusted provenance escalates to
CONFIRM (`policy.decide`), and values such as commands read from a screenshot are
tagged `untrusted_source` in the confirm rows. HANDS never learns skills from
untrusted-triggered tasks (`learned=null`).

## 2. World model — `hands.world`

A live, cheap index. Refreshed on demand and incrementally; per-section freshness
is reported. Queryable by structured filter (no model call).

```python
hands.world_query(select, where=None, *, refresh="auto", max_age_s=2.0, limit=50) -> Receipt
hands.world_snapshot(*, refresh="auto") -> Receipt   # everything, one object
```

`select` is one of `apps | windows | tabs | terminals | media | system | focus | displays`.
`where` is a dict of exact or `*_contains` filters over the item fields below.

| select | item fields |
| --- | --- |
| `apps` | `name, bundle_id, pid, frontmost, hidden, scriptable` |
| `windows` | `id, app, title, frame{x,y,w,h}, display, minimized, front, space?` |
| `tabs` | `browser, window_id, tab_id, index, title, url, active, audible, playing, media_kind?, incognito` (incognito and excluded hosts are omitted, see exclusions) |
| `terminals` | `app, window_id, tab_id, terminal_id, title, cwd, foreground_cmd?, pid?, session_hint?` (Ghostty native terminals, tmux panes on the `ev-*` sockets) |
| `media` | `app, window_id?, tab_id?, title, url?, state(playing/paused/ad), position_s?, duration_s?, ad{active,skippable,skip_in_s,remaining_s}?` |
| `system` | `screen_locked, display_asleep, idle_s, volume, muted, on_battery, focus_mode?, clamshell?` |
| `focus` | `app, window_title, element?{role,name}` (secure fields reported as `role:"secure"` with no value) |
| `displays` | `id, frame, main` |

Example: `hands.world_query("tabs", {"playing": True, "url_contains": "youtube.com"})`.

**Exclusions** (default, from `Settings`, not overridable by the brain): password
managers, banking hosts, private/incognito windows, and secure text fields never
appear in `tabs`/`focus` evidence. The receipt reports `"excluded": N` only.

## 3. UI primitives — `hands.ui`

Element identity is a **semantic selector**, never raw coordinates:

```json
{"app": "Google Chrome", "window": {"title_contains": "YouTube"}, "role": "button",
 "name": "Skip", "ancestry": ["player"], "nth": 0}
```

```python
hands.ui_find(selector, *, channels=None, limit=10) -> Receipt
  # evidence.matches = [{ref, source:"ax|dom|ocr|app", role, name, value?, frame?, confidence, selector}]
hands.ui_act(target, action, args=None, *, verify=None, restore_focus=True,
             provenance="owner", approved=False) -> Receipt
  # target: a ref from ui_find (valid for the observation_id it came from) or a selector dict
  # action: press | type | set_value | scroll | select | menu | key | focus | pointer
hands.ui_wait_until(predicate, *, timeout_s=10.0, poll_s="adaptive") -> Receipt
  # predicate kinds: ax{selector,exists|value}, dom{tab,js_expr|selector}, ocr{text,region?},
  #                  process{name,running}, file{path,exists|min_bytes}, world{select,where}
hands.ui_observe(scope, *, include_image=False) -> Receipt
  # structured snapshot of an app/window/tab; image only if requested
hands.ui_verify(postcondition) -> Receipt
  # postcondition uses the same grammar as ui_wait_until.predicate (single check, no waiting)
```

The channel ladder is applied in this order and the receipt names the rung used:
app scripting/API -> browser DOM JavaScript (only where the browser permits it;
otherwise `js_disabled` with the exact setting) -> Accessibility tree -> keyboard
shortcuts -> pointer actions on **located** elements -> OCR-located targets ->
brain vision (last resort, one image, synthetic/owner-approved only). `pointer`
and `ocr` actions require the element to have been *located in the same
observation*; a bare `{x,y}` is rejected (`invalid_args`).

`restore_focus=true` returns the owner's previous frontmost app/window after the
action when the task did not need focus. AFK-safe media tasks never take focus
when the structured channel works.

## 4. Skills — `hands.skill`

```python
hands.skill_list() -> Receipt                      # all packs/skills + api version
hands.skill_find(intent, *, context=None, limit=5) -> Receipt
hands.skill_run(name, args=None, *, dry_run=False, provenance="owner",
                approved=False, approval_id=None, deadline_s=None) -> Receipt
```

`skill_find` is deterministic (manifest triggers + live preconditions; no model):

```json
{"evidence": {"skills": [
  {"name": "web_media.skip_ad", "score": 0.92,
   "why": "intent mentions ad; a YouTube tab is playing",
   "args_schema": {"type": "object", "properties": {"tab": {"type": "string"}, "wait_s": {"type": "number"}}},
   "preconditions": [{"check": "browser_running", "ok": true}, {"check": "js_allowed", "ok": true}],
   "permissions": ["automation:Google Chrome"],
   "risk": "low", "status": "stable", "channels": ["dom", "ax"]}]}}
```

`dry_run=true` returns `plan` (the ordered steps with resolved targets and, for
terminals, pane -> cwd -> command) and `confirm` when the real run would need it,
with **no effect**. `skill_run` of a skill whose preconditions fail returns
`precondition_failed` (or the more specific code) and says which.

Skill packs shipped in 1.0.0 (names are stable; arguments are in each manifest's
`args_schema`, returned by `skill_find`/`skill_list`):

| Pack | Skills |
| --- | --- |
| `ghostty` | `windows_list`, `new_tab`, `new_window`, `split`, `send_input`, `read_pane`, `focus`, `close`, `layout_save`, `layout_restore`, `new_tab_with_layout` |
| `coding_sessions` | `discover`, `list_recent`, `validate_id`, `resume_command`, `restore_from_screenshot` (T1), `detect_running` |
| `web_media` | `status`, `play`, `pause`, `seek`, `volume`, `next`, `previous`, `skip_ad` (T2), `ad_state`, `captions`, `speed` |
| `browser` | `tabs_list`, `tab_find`, `tab_open`, `tab_close`, `tab_switch`, `tab_reopen`, `read_page`, `fill_form` (benign fields only), `reopen_from_screenshot` |
| `finder` | `reveal`, `select`, `move`, `rename`, `tag` |
| `system` | `volume`, `mute`, `brightness`, `focus_mode`, `sleep_display`, `lock_state` |
| `music` | `play`, `pause`, `next`, `previous`, `now_playing` (Music/Spotify if installed) |
| `calls` | `mute`, `unmute`, `leave` (Zoom/Meet/FaceTime if installed) |
| `editors` | `open_folder`, `open_file`, `goto_line` (VS Code/Cursor if installed) |
| `learned.*` | compiled skills, receipt carries `learned:{status:"learned_unreviewed"}` |

## 5. Watchers — `hands.watcher`

Event-driven (or adaptive-polling) standing tasks with scope, expiry and a cost cap.

```python
hands.watcher_create(spec, *, provenance="owner", approved=False) -> Receipt
hands.watcher_list(*, include_finished=False) -> Receipt
hands.watcher_cancel(watcher_id) -> Receipt
hands.watcher_events(since=None, watcher_id=None) -> Receipt   # pull; also pushed via the sink
hands.set_event_sink(fn)    # fn(event: dict) -> None; CLI 1 routes to thread + channel (B5)
```

```json
{"kind": "skip_ads", "scope": {"tab_url_contains": "youtube.com"}, "expires_in_s": 3600,
 "max_actions": 200, "cost_cap": {"cpu_pct": 1.0, "model_usd": 0}}
```

Preset kinds: `skip_ads`, `file_done` (download/build output stops growing),
`process_exit`, `tab_state`, `custom` (condition = predicate grammar of
`ui_wait_until`, action = a `skill_run` call). A watcher may only take **low-risk**
actions on its own; any other action emits an event `needs_confirm` and waits.
Events: `{id, watcher_id, t, type: "fired|expired|cancelled|needs_confirm|error", receipt?, say?}`.
No model call is made per tick. Idle watchers cost no measurable CPU.

## 6. Screenshot-as-reference — `hands.screen`

```python
hands.screen_reference(image, target, *, hint=None, provenance="screenshot") -> Receipt
# image: "last" | file path | artifact id.  target: terminal_layout | browser_tabs |
#        window_arrangement | settings_toggles
hands.screen_apply(plan_id, *, approved=False, approval_id=None) -> Receipt
```

`screen_reference` **never acts**. It returns a grounded plan (`plan`, `confirm`,
`plan_id`) built from: brain vision for structure (injected callable), OCR for exact
strings, grounding against local truth (coding-CLI session stores, filesystem,
browser history only where the owner allowed it), and per-field flags
(`exact | corrected | unreadable | unverified`). `screen_apply` executes the plan
exactly as confirmed and verifies by reading each target back. Anything the grounding
could not confirm is **flagged and not executed**.

Split vocabulary is Ghostty's: `dir: "right"` = side by side (`a` is left), `"down"` = stacked (`a` is top);
`ratio` is `a`'s share of the pair. A layout is a guillotine tree; `app.fabric.skills.layout_tree.canonicalize`
turns any equivalent tree into a right-leaning chain so every divider can be moved by one `resize_split`.

T1 plan shape (`target="terminal_layout"`; the skill form is `coding_sessions.restore_from_screenshot`):

```json
{"layout": {"kind": "split", "dir": "right", "ratio": 0.5, "a": {"kind": "pane", "id": 1}, "b": {"kind": "split", "dir": "down", "ratio": 0.5, "a": {"kind": "pane", "id": 2}, "b": {"kind": "pane", "id": 3}}},
 "panes": [{"id": 1, "cwd": "/Users/x/Code/ev", "cli": "codex", "session_id": "01a02222-84cd-7d73-9c85-22ec969143b1", "ocr_id": "01a02222-84cd-7d73-9c85-22ec969l43b1", "id_status": "corrected", "command": "codex resume 01a02222-84cd-7d73-9c85-22ec969143b1", "running_elsewhere": null}],
 "destination": {"app": "Ghostty", "mode": "new_tab", "window": "front"}}
```

## 6b. Learning — `hands.trace_begin / trace_end`

```python
hands.trace_begin(task, *, provenance="owner", params={"name": "Ada"})   # record what the brain does next
... hands.ui_act(...) / ui_wait_until(...) / ui_verify(...) ...            # successful steps are recorded
hands.trace_end(replay_setup=fn, params_for_replay={"name": "Grace"})     # compile, replay on a fresh instance, store
```

Only a trace that ended in a VERIFIED postcondition, started by the owner (never `web/email/message/screenshot/ocr/
audio/terminal_output` provenance), compiles. The compiled skill is `learned.<slug>`, `status:"learned_unreviewed"`
(every receipt carries `learned:{skill,status,version}`), parameterised (`{name}`), selectors only (no coordinates), and
is stored in `<state dir>/learned/*.json` (`~/.ev/hands/learned`), **not** in the recipes/routines DB: recipes replay
`ev.tools` dispatch steps, learned skills replay UI steps. CLI 1 may wrap one as a recipe step calling `skill_run`.
A step whose selector no longer matches is repaired from the live page (closest same-role name, else the control in the
same place among the same number of same-role controls), re-verified, and saved as a new version with its history.
Learned skills are `risk:"confirm"`: the first call returns a confirm card.

## 7. Diagnostics — `hands.diag`

```python
hands.permissions() -> Receipt     # Accessibility, Automation per app, Screen Recording, JS-from-Apple-Events
hands.selftest(pack=None) -> Receipt   # each pack's built-in fixture self-test (safe, own windows)
hands_api_version() -> "1.0.0"
```

## 8. Integration notes for CLI 1

* Suggested model-facing tools (names are yours): `computer.world`, `computer.find`,
  `computer.act`, `computer.skill` (find/run), `computer.watch`, `computer.screen_reference`.
  Keep `skill_find` -> `skill_run` as the dominant path; primitives are for novel tasks.
* Feed `say` back as the sentence candidate; feed `confirm` to the confirm card; echo
  `provenance` of anything the model quoted from a screenshot/page.
* Take the GUI lock (`docs/contracts/GUI_LOCK.md`) for any live-GUI test; the API itself
  does not take it (production use runs on the owner's real desktop).
* Tool results the brain sees: send `say`, `ok`, `verified`, `error{code,message,fix}`, `needs_confirm`, `confirm` and a
  trimmed `evidence`; keep `steps` for logs. Anything under `evidence` marked `untrusted:true` came from a page, a
  terminal or a screenshot and must stay quoted data in the prompt.
* `skill_find` is a deterministic prior (92% top-1, 96% top-3 over the CUG phrasings), not a decision: "turn it up" is
  ambiguous between `system.volume` and `web_media.volume`; use `world_query("media")` to disambiguate.
* Watchers deliver events through `set_event_sink` (and `watcher_events`); the host wakes every ~10 s, the skip itself
  happens in the page at once. `cancel` reports what the watcher did ("It skipped 2 ads.").
* `Hands()` with no arguments is production wiring: system runtime, bridge-first for third-party app scripting when
  Evie.app answers (`EV_HANDS_OSASCRIPT_ROUTE`), `~/.ev/hands` state. The native helper builds itself once with `swiftc`
  from `helpers/evhands` (the Evie.app bridge can launch the same binary under its own grants).
* Settings added by HANDS are all `EV_HANDS_*` fields in `Settings`, `.env.example` and
  `docs/ENVIRONMENT.md` in a `# --- AGENT 4 HANDS ---` block (no bare `os.getenv`).
* Nothing in this package reads, prints or stores a secret. Model access (for vision) is
  an injected callable `vision_fn(image_bytes, prompt, *, json_schema=None) -> dict`; the
  default adapter uses the same provider role the gateway uses and the egress ledger.
