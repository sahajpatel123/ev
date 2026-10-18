# iPhone + Mac Executor Plan — Owner-Messaging & Desktop-App Lanes

**Status:** plan only. No code changed to write this file.
**Date:** 2026-10-10. **Tree:** `/Users/sahajpatel/Code/ev`.
**Evidence rule:** every grounded claim cites a file read this session.
Prior results 1–5 arrived as `structured result submitted` with **no
inspectable evidence bodies**, so nothing below reuses them; all grounding
is from direct reads listed in §7. Unresolved items from the caller are
reproduced verbatim in §6.

**Conflict flag (up front):** the task instruction says "Read fully: prior
result 1 … 5" but the runtime delivered only one-line summaries with no
content. This plan therefore treats all prior-result content as **missing**,
not as verified. If the full prior results exist elsewhere, §§1–5 need a
reconciliation pass before build starts.

## 0. Mission + non-goals

### Mission
Give the owner's iPhone (Safari PWA at `/evie/` over Tailscale — primary
path per `docs/IPHONE_PRODUCT.md`) honest read / draft / send agency over
owner messaging and desktop-app surfaces, executed on the Mac Home Station
through typed adapters with delivery evidence, standing authority, and loud
failures. Lanes: iMessage/SMS, Apple Mail, Gmail (native API + web
fallback), WhatsApp (Desktop cache + AX + headless CDP), Telegram Desktop,
Signal Desktop, Slack, Discord, Teams/Zoom meetings joins, Messenger/IG DMs.

### Non-goals (binding)
- No multi-user / guest mode; single owner only (`AGENTS.md` §1,
  `docs/PERMISSIONED_OPERATING_LAYER.md` authority model).
- No Domain 20, no new product domain (`AGENTS.md` §1).
- No theatrical capability claims: missing provider → `not_connected` /
  `degraded=true`, never simulated success (`docs/POL_IMPLEMENTATION_ROADMAP.md`
  Phase 0, `docs/GATEWAY.md` §3).
- No raw terminal / arbitrary command actuator as first resort
  (`docs/PERMISSIONED_OPERATING_LAYER.md` §Computer Control).
- No R3/R4 (call, physical, irreversible, financial) on voice-wake auth
  alone; needs independent confirmation factor (POL risk table).
- No secrets to the model; no keys in app bundles
  (`docs/POL_DEPENDENCIES_AND_LIMITS.md`, `docs/INTEGRATIONS.md` §9).
- No phone-side Mac **control** (`ui_action`, `app_action`, `computer`):
  `backend/app/device_gateway/phone_mac.py:48-67` blocks them and Phase 4b
  (`docs/IPHONE_CAPABILITY_PLAN.md` §Phase 4b) deliberately exempts only
  read-only observe (`screen_look`, `inspect_ui`) for trusted owners.
  New lanes must not smuggle control through the phone lane.
- No silent mark-read, no mutating a third-party SQLite DB in place, no
  claiming cache contents are live (established by
  `backend/app/ev/messaging/whatsapp_local.py:1-6`).

## 1. Lane designs

Conventions used in every lane: **input path** (how a turn/request reaches
the executor), **output path** (evidence shape returned), **Mac prereqs**,
**TCC/permissions**, **failure taxonomy** (typed code → owner-visible
message → next step).

Shared authority facts all lanes inherit:
- Pair lands sandbox; only master-key `POST /admin/promote-owner` flips to
  owner scope and bumps `auth_revision` (`docs/IPHONE_CAPABILITY_PLAN.md`
  §messages-auth; `backend/app/device_gateway/auth.py` per same doc).
- Life standing authority: scopes `messaging:read/act`, `phone:act`,
  `contacts:read`; allowlist `EV_LIFE_CONTACT_ALLOWLIST` =
  all|starred|any; unknown recipients need `confirm:true` when
  `EV_LIFE_CONFIRM_UNKNOWN=true` (`docs/INTEGRATIONS.md` §15.3).
- Gateway autonomy `EV_OWNER_AUTONOMY=full|confirm_unknown|confirm_all`,
  default `full` (`docs/GATEWAY.md` §3).
- Helper wire contract: `EVLifeHelper <command> [--flag value …]`, one JSON
  object on stdout, exit 0/1/3/4/5
  (`backend/app/integrations/life_helper.py:1-24`,
  `macos/Sources/EVLifeHelper/main.swift`).
  Delivery commands require `data.sent/opened == true`; `dry_run` is never
  delivery (`life_helper.py:46-72`).
- Offline suite must stay green with doubles setting `degraded=true`
  (`AGENTS.md` §3).

### Lane 1 — iMessage / SMS (read + send)

**Input path.** Phone turn → Device Gateway → `send_message` /
`list_messages` life tools (`docs/GATEWAY.md` §3 table) → integrations
`messaging.send` / `messages.list` with `provider=macos_life`
(`docs/INTEGRATIONS.md` §15.1) → `life_helper.py` runner →
`EVLifeHelper messages.list|messages.send`
(`macos/Sources/EVLifeHelper/main.swift:318-399`).

**Output path.** Read: rows `{handle, text, ISO date}` from
`~/Library/Messages/chat.db` (`main.swift:961`ff, INTEGRATIONS §15.2
real-provider evidence). Send: `{to, sent:true, service, headless:true,
focus_stolen:false}` (`main.swift:390-396`); backend wraps as
`delivery:{confirmed:true, evidence:{provider:EVLifeHelper,…}}`
(INTEGRATIONS §15.2). Missing evidence → `missing_delivery_evidence`,
never silent success.

**Mac prereqs.** `EV_LIFE_HELPER_PATH` set to release binary
(`macos/.build/…/EVLifeHelper` or `EV.app/Contents/MacOS/EVLifeHelper`);
Messages app present; `EV_MESSAGING_PROVIDER=macos_life`
(`backend/app/config.py:749-750`); messaging integration installed with
`messaging:read` / `messaging:act` scopes (INTEGRATIONS §15.1 step 4).

**TCC.** Full Disk Access for `chat.db` reads; Automation (AppleEvents) for
`com.apple.MobileSMS` for sends. Helper exits 3 on denial; backend raises
`LifePermissionDeniedError` naming System Settings → Privacy & Security
(`life_helper.py:103-112`; `docs/IPHONE_CAPABILITY_PLAN.md` Phase 3 pins
this in `test_life_bridges.py` 35 passed / 1 skipped). Dry-run compiles the
AppleScript and checks `automationGranted` first (`main.swift:370-386`).

**Failures.**
- `helper_missing` (no `EV_LIFE_HELPER_PATH`) → "Mac helper not installed"
  + install step. `LifeHelperUnavailableError`.
- `permission_denied` (exit 3) → 403 with exact Settings pane.
- `buddy_targeting_failed` — **known-uncertain**: INTEGRATIONS §15.2
  records a real-host failure `Expected class name but found identifier`
  with a DEPENDENCY NOTE to Agent 18 to fix `buddy` targeting in
  `main.swift`. Current `main.swift:334-363` still builds `buddy "<name>"
  of targetService` AppleScript. It is **unverified whether the fix
  landed**; treat first on-device send as the acceptance gate and keep the
  loud 502 (no fake success).
- `sms_relay_off` → SMS `--service sms` fails when the Mac has no
  relay/phone pairing; needs honest wording (see §6 unresolved — wording
  not yet specified).
- `lock_screen_send` / `e2e_after_buddy_note` / `group_targeting`: current
  code has **no group-chat addressing** (single `--to` buddy only) and no
  lock-screen or E2E-verification handling — all three are open build items
  (§6 verbatim). Do not ship group sends by concatenating recipients.

### Lane 2 — Apple Mail (read + send + reply/forward verbs)

**Input path.** Same life-tool path as Lane 1: `list_mail` → `mail.list`,
`mail.send` action (`docs/GATEWAY.md` §3; INTEGRATIONS §15.2 command
table).

**Output path.** Read: `[{subject, sender, received}]` from the Mail
Envelope Index via read-only `/usr/bin/sqlite3 -readonly -separator \t`
(`main.swift:1027-1083`); inbox-only (`WHERE … mb.url LIKE '%/INBOX'`).
Send: AppleScript `make new outgoing message … visible:false … send`
(`main.swift:607-638`); headless launch/quit handling preserves focus.

**Mac prereqs.** `~/Library/Mail` present; `/usr/bin/sqlite3` executable;
Mail account configured; helper path + `messaging`-style mail scopes
(backend mapping per INTEGRATIONS §15.2).

**TCC.** Full Disk Access for Envelope Index reads — failure string names
the FDA pane explicitly (`main.swift:1032-1040`). Automation for
`com.apple.mail` for sends, with dry-run compile check
(`main.swift:617-627`).

**Failures.** `mail_data_absent` (no `~/Library/Mail`) → `not_available`;
`envelope_index_unavailable` → `permission_denied` + FDA steps;
`automation_denied` → dry-run abort message; send AppleScript error → loud
`failed`. **Missing verbs:** live `reply` / `forward` do not exist in the
helper command table (`life_helper.py:74-98` lists only `mail.list` /
`mail.send`); they are new commands to design (thread-ID addressing, quote
body, same delivery-evidence rule). **Missing column:** no read/unread flag
is selected today (SELECT is subject/sender/date only) — adding it needs an
FDA host probe of the Envelope Index schema on the owner's macOS version
(§6 unresolved).

### Lane 3 — Gmail (native API primary; headless web fallback)

**Two paths coexist — conflict flagged.** INTEGRATIONS §4.1/§10 says Gmail
list is metadata-only (Subject/From/Date) over the Google OAuth client and
"send stays on the headless Mail.app helper". Separately,
`backend/app/digital/adapters/gmail.py:1-60` implements a **native Gmail
API adapter** with `gmail.readonly/modify/send/compose` scopes
(`gmail.py:16-19`), superset-aware read guard (`gmail.py:22-32`), and
`CONNECTION_REQUIRED` availability projection (`gmail.py:35-42`).
These are not the same design: one forbids Gmail send, the other declares
write availability. The plan treats the native adapter as the forward path
and the INTEGRATIONS sentence as stale-until-reconciled; do not implement
both send paths.

**Input path (native).** `digital.act` service `gmail` (same family as the
documented `digital.act` whatsapp service, INTEGRATIONS §16 Evie
operations) → `GmailAdapter` → `GmailClient`/`HttpxGmailTransport`
(`gmail.py:11`) with vault-bound OAuth lease (`vault_bound.lease_for`,
`diagnose_oauth_error`).

**Output path.** Reads return message envelopes with untrusted-data
provenance (follow the WhatsApp read contract: source + bounded-history
metadata, INTEGRATIONS §16). Writes follow the bound `life.send` approval
flow (same rule as WhatsApp sends, INTEGRATIONS §16).

**Mac prereqs (native).** None on the Mac — server-side OAuth: Google Cloud
client id/secret/redirect in env (`EV_GOOGLE_OAUTH_*`, INTEGRATIONS §4.2),
Gmail API enabled, `gmail.readonly` consent minimum; `gmail.modify`/`send`
only if the owner approves write scope. Vault holds user tokens; client
credentials stay in env, never in integration config (INTEGRATIONS §4.2–4.3,
§7). Headless-web fallback (if ever needed) would need the CDP workspace
like WhatsApp (§Lane 10) plus a bot/CAPTCHA probe — currently unresolved
(§6).

**TCC.** None for the native path (no local DB). Web fallback would need no
TCC either but inherits all CDP brittleness (below).

**Failures.** `oauth_required` / `reauth_required` (expired/invalid grant →
401 + re-authorize prompt, INTEGRATIONS §4.4); `insufficient_scope` (write
asked on read-only lease → request `gmail.modify`/`send` consent, never
silent downgrade); `headless_bot_challenge` (web fallback only — CAPTCHA /
bot wall → stop + honest message, no credential stuffing, no CAPTCHA
solving).

### Lane 4 — Telegram Desktop (AX map first; bot alerts vs user account)

**Input path (proposed).** New `device_gateway` phone-home capability (add
to `PHONE_HOME_CAPABILITIES`, `phone_mac.py:19-38`) → new integrations
adapter or `digital.act` service `telegram` → EVLifeHelper-style AX
subprocess **or** headless web. No telegram adapter exists today:
`backend/app/digital/adapters/` contains only browser, calendar, contacts,
files, gmail, phone, whatsapp (measured this session); `classify.py` /
`extract.py` mention only meeting regexes. This lane is greenfield.

**Output path.** Reads: `{chat, sender, text, ts, source: telegram_ax |
telegram_web}` + provenance. Sends: same delivery-evidence rule as
`messages.send` (verbatim `sent:true` from in-thread confirmation; focus
restored).

**Mac prereqs.** Telegram Desktop installed + logged in (user-account
route) **or** owner bot token in vault (bot-alerts route — see decision
below). AX route needs the app's AX tree mapped on the owner's build.

**TCC.** Accessibility for the AX route (EVHelper/EV.app in the
Accessibility pane; stale grants need remove/re-add per the Mac-control
ladder, `docs/IPHONE_CAPABILITY_PLAN.md` §mac-control). No FDA (no local
DB read proposed).

**Failures.** `ax_tree_changed` (Telegram update reshuffles AX roles →
loud unavailable + re-map task, never blind clicking); `not_logged_in`;
`bot_scope_insufficient` (bot route can post to configured chats only —
never full inbox read). **Owner decision required:** bot-alerts (narrow,
stable, vault token) vs full user-account AX (broad, brittle). Unresolved
in §6 — do not build both; ask once, build one.

### Lane 5 — Signal Desktop (AX richness; stable compose entry)

Same greenfield status as Lane 4 (no signal adapter in tree).

**Input/output path.** Mirror Lane 4 with service `signal`. Reads need an
AX richness probe first: Signal's Electron AX tree may expose far less than
WhatsApp Desktop's (which has a dedicated `WhatsAppAX.swift`, 592 lines —
the richest AX precedent in the tree). If AX is too poor, the lane stops at
honest `unavailable`, not at screen-scraping pixels.

**Mac prereqs.** Signal Desktop installed + linked. **Stable compose entry
unknown:** whether a `signal://` scheme, CLI, or menu path can open a chat
compose deterministically is unresolved (§6) — probe before designing
sends. No `open_url` from the phone lane (`phone_mac.py` blocks it); entry
must be Mac-side.

**TCC.** Accessibility for AX. Sealed-sender / disappearing-message
semantics must be preserved in reads (note retention, never persist
disappearing content beyond its policy).

**Failures.** `ax_insufficient` → capped lane (alerts only or unavailable);
`compose_entry_missing` → reads-only lane; `link_required`.

### Lane 6 — Slack (Electron AX depth; admin/DLP; mark-read side-effects)

Greenfield (no slack adapter in tree).

**Input path (proposed).** Prefer official Slack API (OAuth, vault tokens)
over AX: Slack has a real API unlike Signal/Telegram-user. `digital.act`
service `slack` → HTTP transport with workspace token. AX only if the owner
refuses API tokens — then same shape as Lane 4.

**Output path.** Reads: `{workspace, channel, user, text, ts, thread_ts}`.
Writes: post/reply with `ts` receipt as delivery evidence. Reactions/threads
are explicit verbs, not side effects.

**Mac prereqs (API route).** Slack app OAuth: workspace install, scopes
(`channels:history`, `chat:write`, … least-privilege), tokens in vault
(per INTEGRATIONS §3 vault rules). AX route: Slack Desktop + Accessibility.

**TCC.** None for API route. Accessibility for AX route.

**Failures.** `workspace_admin_block` (admin disallows integrations/DMR);
`dlp_hold` (DLP/export policy restricts read or post → refuse + cite
policy, never bypass); `ax_mark_read_side_effect` — **hard rule:** any
desktop-AX read that marks messages read as a side effect fails the lane's
acceptance; reads must be non-mutating (same principle as
`whatsapp_local.py` header). All three need owner-workspace probing (§6).

### Lane 7 — Discord (live AX walkability; bot scope; deep links)

Greenfield (no discord adapter in tree).

**Input path (proposed).** Two candidate routes, pick one after probing:
(a) bot with explicit server scope (least privilege, stable API, vault
token); (b) desktop-AX walk of Discord Electron (brittle, ToS-sensitive —
automating a **user** account via self-bot/API is against Discord ToS; AX
of the owner's own client is the only non-bot route considered, and it must
not exfiltrate other users' DMs beyond the owner's explicit request).

**Output path.** Reads: `{guild|dm, channel, author, text, ts}` + bounds.
Writes: bot post with message-id receipt; AX send only with in-thread
confirmation.

**Mac prereqs.** (a) Bot invited to named servers with named scopes. (b)
Discord Desktop installed + logged in + AX-walkable.

**TCC.** Accessibility for route (b) only.

**Failures.** `bot_scope_missing` (server not in bot allowlist → refuse);
`ax_unwalkable` (Discord AX roles insufficient → lane unavailable, no
pixel-clicking fallback); `deep_link_unsupported` (`discord://` channel-nav
caps unverified — §6 — probe before relying on it for targeting).

### Lane 8 — Meetings: Teams / Zoom (scriptable mute/join; local recording)

Greenfield as an executor lane (only meeting-word regexes exist in
`digital/classify.py:15`, `digital/extract.py:21`).

**Input path (proposed).** Read-only calendar-join first: calendar
`next_event` signals already yield meeting links (INTEGRATIONS §4.3
`participants`/signals); new `meet.join` verb opens the link Mac-side and
reports join evidence (window/process observed). Mute control only via a
scriptable, vendor-supported API — not via blind AX clicks during a live
call.

**Output path.** Join: `{meeting, link, joined_at, evidence:{process|
window|url_opened}}`. Mute: `{muted:bool, evidence}` or honest
`mute_unavailable`. Recording: path receipt only.

**Mac prereqs.** Teams and/or Zoom Desktop installed + signed in; calendar
integration synced (daily sync per INTEGRATIONS §14 Agent-20 note).

**TCC.** Accessibility + Screen Recording only if AX/window evidence is
used; Microphone/Camera belong to the meeting apps, not EV.

**Failures.** `no_scriptable_mute` (neither app exposes one → lane offers
join-only + spoken "mute in the app" guidance); `join_api_missing`;
`recording_path_unknown` (Zoom local-recording path varies — §6 — probe
`~/Documents/Zoom` + Zoom prefs on the owner's Mac before promising
transcripts). Never auto-record without consent + announced policy.

### Lane 9 — Social DMs: Messenger / Instagram (headless, checkpoint-prone)

Greenfield; highest bot-risk lane. No messenger/instagram adapter in tree.

**Input path (proposed).** Headless CDP workspace mirroring the WhatsApp
CDP pattern (isolated profile `~/.ev/chrome-cdp`-style, 127.0.0.1 debug
port, serialized navigation, visible/mismatched-target refusal —
INTEGRATIONS §16). One service per surface (`messenger`, `instagram`) so
checkpoint state is isolated.

**Output path.** Reads with untrusted-data provenance + bounded history
(same contract as WhatsApp Web reads, INTEGRATIONS §16). Drafts are not
sends; sends need `life.send` approval + new-outgoing-message verification,
never auto-retry on uncertainty.

**Mac prereqs.** Chrome available; isolated CDP profile per surface; owner
logged in inside that profile (EV never handles Meta credentials — owner
types them in the visible linking step only).

**TCC.** None. The gate is Meta's, not Apple's: login checkpoints, 2FA,
"unusual browser" challenges.

**Failures.** `meta_checkpoint` (challenge wall → stop, surface exact step
to the owner, never solve CAPTCHA/2FA); `selector_drift`
(messenger.com/instagram.com DOM changed → pinned-selector tests fail loud,
lane pauses until re-pinned); `session_logged_out`. Checkpoint
**persistence** in headless across restarts is explicitly unresolved (§6) —
probe session-restore before promising background operation.

### Lane 10 — WhatsApp (three transports; companion-device limit)

The only lane with all three transports already in the tree. **Ordering is
locked:** `desktop_cache` first, CDP second, AX where it fits; sends stay
inside the `life.send` approval flow
(`docs/IPHONE_CAPABILITY_PLAN.md` Phase 3; INTEGRATIONS §16).

- **Transport A — Desktop cache (read-only).**
  `backend/app/ev/messaging/whatsapp_local.py` reads
  `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`
  in `mode=ro` + `PRAGMA query_only=ON` (`whatsapp_local.py:18,42-58`),
  schema-pinned to `ZWACHATSESSION`/`ZWAMESSAGE`, broadcast/newsletter
  filtered (`whatsapp_local.py:61-75`). Never marks read, never claims live
  (`whatsapp_local.py:1-6`, `_snapshot` freshness
  `desktop_sync_unknown`). Needs WhatsApp Desktop installed + synced and
  **FDA on ChatStorage** — doc path + System Settings steps are an open doc
  item (§6). WAL-aware snapshot (`whatsapp_local.py:33-39`).
- **Transport B — Headless CDP Web (read + send).**
  `backend/app/ev/messaging/whatsapp_cdp.py` + `BackgroundWhatsAppBacking`
  (`backend/app/digital/adapters/whatsapp.py:486-552`) over an isolated
  profile (default `~/.ev/chrome-cdp`, 127.0.0.1:9222; overrides
  `EV_WHATSAPP_CDP_PROFILE`/`EV_WHATSAPP_CDP_PORT`, INTEGRATIONS §16).
  Link state machine: `linked | qr | loading | unavailable`
  (`whatsapp_cdp.py:365-402`); QR via owner-key `GET
  /v1/digital/whatsapp/link-qr` (no-store PNG) or CLI `--qr` to an
  owner-only file (INTEGRATIONS §16). Sends verify a new outgoing message
  before claiming success; uncertain sends never auto-retry (INTEGRATIONS
  §16). **Companion-device limit / eviction handling is unresolved** (§6):
  WhatsApp caps linked devices (~4); the CDP profile can be evicted by a
  newer link. Code + doc must define: detect eviction (`cdp_not_linked`
  after previously linked + marker at `whatsapp_cdp.py:67-75`), speak exact
  re-link steps, never auto-evict another device, never fight the owner for
  a slot.
- **Transport C — Desktop AX (background, never activates).**
  `EVLifeHelper whatsapp.ax_*` (`life_helper.py:91-98`,
  `main.swift:461ff`, `WhatsAppAX.swift` 592 lines):
  `ax_status|ax_request_access|ax_chats|ax_open|ax_read|ax_send`.
  `ax_send` returns `sent:true` only after in-thread composer confirmation
  and restores focus (`life_helper.py:51-54,63-68`). Compose-only
  `whatsapp.send` (`whatsapp://` URL, `main.swift:401-436`) opens a compose
  surface and is **never delivery evidence** (`life_helper.py:56-61).

**Mac prereqs.** A: Desktop installed + synced. B: Chrome + linked CDP
profile. C: Desktop + Accessibility grant (`ax_request_access`).

**TCC.** A: FDA (ChatStorage). B: none. C: Accessibility. No lane needs
more.

**Failures.** `desktop_cache_unavailable` (open Desktop to sync);
`session_logged_out`/qr (link-device steps);
`chat_not_found`/`ambiguous_recipient` (name the chat, never "connection")
— all surfaced by `_whatsapp_read_error_message()` per Phase 3
(`docs/IPHONE_CAPABILITY_PLAN.md`); `dead_cdp_transport`;
`companion_evicted` (new code, see above); `qr_expired` (new watcher, see
§2).

## 2. Shared platform

Build once, reuse in every lane. All items are additive; no existing
endpoint signature, table column, or setting default moves (`AGENTS.md`
§3 shared-file rule).

1. **Typed error + spoken-message layer.** Extend the Phase-3
   `_whatsapp_read_error_message()` pattern (adapters.py per
   IPHONE_CAPABILITY_PLAN) to a per-lane `*_read_error_message()` /
   `*_send_error_message()`: typed code + one honest spoken line + one
   next step. Codes: `helper_missing`, `permission_denied`,
   `not_connected`, `session_logged_out`, `qr_expired`,
   `companion_evicted`, `chat_not_found`, `ambiguous_recipient`,
   `ax_unwalkable`, `checkpoint`, `selector_drift`, `scope_missing`.
2. **Delivery-evidence gate.** Every send verb returns
   `delivery:{confirmed, evidence:{provider, confirmed_by, ids, ts}}` or
   fails with `missing_delivery_evidence` (INTEGRATIONS §15.2). Compose /
   draft / opened-URL states can never satisfy it
   (`life_helper.py:56-61`). Add a shared backend assertion helper so new
   lanes cannot forget it.
3. **Read-only / non-mutating discipline.** New read paths prove
   `marks_read:false` (SQLite: `mode=ro` + `query_only`, cf.
   `whatsapp_local.py:42-58`; AX: no click-through of unread threads; web:
   no send/ack actions in read flows). Acceptance tests assert it (§3).
4. **CDP workspace supervisor.** Generalize the WhatsApp CDP profile
   pattern (isolated profile dir, 127.0.0.1 debug port, cross-process
   navigation serialization, visible/mismatched-target refusal —
   INTEGRATIONS §16) into one supervisor used by WhatsApp + future
   Messenger/IG/Gmail-web lanes. **New, unresolved:** `launchd` supervisor
   plist + QR-expiry watcher for the CDP workspace (§6): keepalive Chrome
   with the debug port, watch QR PNG age, expire + notify instead of
   serving a stale QR. Existing launchd surface is
   `launchd/ev.{api,collector,ears,hands_free,runtime,scheduler,talk,worker}.plist`
   — the supervisor plist joins that family, installed via
   `launchd/install.sh`.
5. **Untrusted-data provenance.** Every external read carries
   `{source, timestamp, bounded_history}` and is marked untrusted for the
   model (WhatsApp precedent, INTEGRATIONS §16; POL memory rule that model
   guesses are never written as facts,
   `docs/PERMISSIONED_OPERATING_LAYER.md` §Memory).
6. **Policy + audit.** All lanes ride `evaluate_policy()` / standing
   scopes / `life/policy` pre-checks (POL Phase 0; INTEGRATIONS §15.3,
   §15.5) and write the POL audit record (actor, device, session, scope,
   evidence — PERMISSIONED_OPERATING_LAYER §Audit). R2 sends need owner
   standing scope or confirmation; nothing here is R3/R4 except calls.
7. **Doubles + skip discipline.** Each lane ships a hermetic double
   (`FakeWhatsAppBacking` at `digital/adapters/whatsapp.py:86` is the
   template) and weight/artifact-dependent tests `skip, never fail`
   (`AGENTS.md` §3).
8. **Freshness watchers (poll first).** `chat.db`, Envelope Index, and
   ChatStorage change under EV. **FSEvents vs poll is unresolved** (§6):
   default to bounded polling (cheap SQLite `mtime`/rowversion checks like
   `whatsapp_local.py:33-39`) until an FSEvents probe proves delivery,
   coalescing, and sleep behavior on the owner's Mac. Never hold a write
   lock on a third-party DB.

## 3. Phased build order + on-device acceptance tests

Order follows POL's roadmap (authority trunk → voice honesty → read-only →
life I/O → one actuator at a time; `docs/POL_IMPLEMENTATION_ROADMAP.md`)
and the iPhone unlock ladder (`docs/IPHONE_PRODUCT.md` checklist). Each
phase ends with its gate; no phase starts until the previous gate is green.

### Phase A — Harden what exists (iMessage + Apple Mail + WhatsApp reads)
1. iMessage send acceptance on the owner's Mac: promoted phone →
   `messages.send --dry-run` (compile + automation check) → real send to a
   consenting test contact → `sent:true` receipt. Decides the
   buddy-targeting question (§1 Lane 1) empirically.
2. Apple Mail read acceptance: `mail.list` returns real inbox rows; FDA
   denial produces the exact Settings path. Envelope Index read-flag probe
   (§6) runs here.
3. WhatsApp `desktop_cache` acceptance: Desktop synced → recent-thread read
   with `marks_read:false` and `desktop_sync_unknown` freshness; Desktop
   quit → `desktop_cache_unavailable` with "open Desktop to sync".
4. Shared error-message + delivery-gate helpers (§2.1–2.2) land here.
- **Gate:** `test_life_bridges.py` + WhatsApp adapter suites green;
  three on-device receipts attached; buddy-targeting verdict recorded.

### Phase B — WhatsApp background sends + supervision
1. CDP link ceremony: `scripts/whatsapp_connection.py --connect`,
   `--qr` PNG, phone Linked-devices link, re-check (INTEGRATIONS §16).
2. Send acceptance: draft → `life.send` approval → verified new outgoing
   message; uncertain send → no retry + honest report.
3. Companion-eviction handling + QR-expiry watcher + launchd supervisor
   plist (§2.4, §6 items) land and are tested by forced-expire / forced-logout.
4. ChatStorage FDA doc (path + Settings steps, §6) ships.
- **Gate:** link → send → evict → re-link cycle demonstrated; stale QR is
  never served; launchd supervisor survives reboot.

### Phase C — Apple Mail verbs + Gmail native
1. `mail.reply` / `mail.forward` helper commands (thread addressing, quote,
   delivery evidence) + backend mapping.
2. Gmail native adapter wiring to vault OAuth + `digital.act`; read
   acceptance (metadata list), then write-scope decision with the owner.
   Reconcile-or-retire the INTEGRATIONS "send stays on Mail.app" sentence.
3. Gmail-web headless probe **only if** native is blocked (bot/CAPTCHA
   verdict, §6).
- **Gate:** reply/forward receipts; Gmail read-or-honest-`CONNECTION_REQUIRED`;
  no dual-send-path ambiguity left in docs.

### Phase D — One new desktop lane (Telegram OR Signal, owner picks)
1. AX map probe on the owner's build (roles, chat list, thread, composer).
2. Reads-only acceptance with `marks_read:false` proof.
3. Compose-entry probe → sends only if a stable, non-focus-stealing entry
   exists (Signal scheme/CLI question, §6).
- **Gate:** one lane fully honest (reads + sends, or reads-only with sends
  explicitly unavailable). No second lane starts until this gate holds for
  two weeks (POL Phase-4 dogfood rule).

### Phase E — Slack, then Discord (API/bot first, AX only on refusal)
1. Slack API route: workspace install → history read → post with `ts`
   receipt; admin/DLP verdicts recorded; AX mark-read probe if AX is used.
2. Discord bot route: scoped server invite → read → post with message-id;
   AX walkability + `discord://` probes only if the bot route is refused.
- **Gate per lane:** receipts + honest `scope_missing` / `admin_block`
  paths; no user-account API automation anywhere (ToS).

### Phase F — Meetings join + Social DMs (last, flakiest)
1. Calendar-link join acceptance (Teams, Zoom) with join evidence; mute
   API verdict; Zoom recording-path probe.
2. Messenger/IG headless acceptance only after checkpoint-persistence and
   selector probes (§6) pass; otherwise the lanes stay `unavailable` with
   dated probe notes instead of half-working automation.
- **Gate:** join receipts; Social DM lanes either verified end-to-end or
  explicitly parked — no silent partials.

### Standing acceptance rules (every phase)
- Promoted-phone ladder test (`test_pair_promote_reconnect_then_turn_ladder`,
  IPHONE_CAPABILITY_PLAN Phase 2) stays green.
- Pair-always-sandbox, stale-lease rejection, WhatsApp-sends-inside-`life.send`,
  offline-suite-green invariants (`docs/IPHONE_CAPABILITY_PLAN.md` §3) hold.
- Every new verb has a real-entry-point test through its factory, never a
  reimplementation inside the test (`AGENTS.md` §7).
- `make lint typecheck test` no worse than the `AGENTS.md` §6 baseline;
  baseline numbers re-measured (`make baseline-write`) if moved.

## 4. Honest limits

- **Phone lane cannot control the Mac.** Observe-only (`screen_look`,
  `inspect_ui`) for trusted owners; `ui_action`/`app_action`/`computer`
  stay blocked (`phone_mac.py:48-67`, IPHONE_CAPABILITY_PLAN Phase 4b).
  Any lane design needing phone-side clicks is invalid.
- **Sends need their Mac-side truth.** iMessage/SMS need Messages +
  Automation (+ SMS relay for SMS); Mail needs Mail + Automation; WhatsApp
  CDP needs a linked slot; Desktop-cache reads need a synced Desktop app.
  Absence is `not_connected`, not a queue-and-hope.
- **Caches are not live.** `desktop_sync_unknown` (WhatsApp),
  inbox-only/no-read-flag (Mail), no group addressing (iMessage) — the
  plan adds probes and verbs but promises none of them until the on-device
  gates pass.
- **Web automation is brittle by nature.** WhatsApp Web UI drift, Meta
  checkpoints, Google bot walls, Discord/Slack Electron AX drift: every
  web/AX lane can regress on a vendor update. The mitigation is pinned
  selectors, loud `selector_drift`/`ax_unwalkable` failures, and
  API/bot-first routing where one exists — not resilience theater.
- **No lanes for strangers' data.** Telecom backdoors, reading strangers'
  messages, city-scale surveillance, tracking without consent, and
  credential theft are permanently out of scope
  (`docs/POL_DEPENDENCIES_AND_LIMITS.md` §Features That Should Not Be Built).
- **Recording and medical-adjacent behavior** need consent + review
  (POL_DEPENDENCIES_AND_LIMITS privacy + safety-bound rules). Meeting
  recording stays off until both exist.

## 5. Effort

Rough, single-engineer, owner-available-for-ceremonies assumptions. S/M/L =
~days / ~1–2 weeks / ~3+ weeks including tests + docs + on-device gates.
Probe spikes are time-boxed: a NO-GO verdict is a completed outcome.

| Work | Size | Notes |
|---|---|---|
| Phase A: iMessage/Mail/WA-read hardening + shared error/evidence helpers | M | Mostly exists; acceptance + buddy verdict + read-flag probe dominate |
| Phase B: WA CDP send hardening + eviction + QR watcher + launchd plist + FDA doc | M | Supervisor plist + watcher are new; ceremony needs the owner present |
| Phase C: mail reply/forward + Gmail native + doc reconciliation | M | New helper verbs; OAuth ceremony; kills the send-path ambiguity |
| Phase D: one of Telegram/Signal AX lane | L | Greenfield AX map + reads + compose probe; high vendor-drift risk |
| Phase E: Slack API + Discord bot (+ AX probes if refused) | M–L | API routes are M; AX fallbacks push to L |
| Phase F: Teams/Zoom join + Messenger/IG headless | L | Flakiest; may end parked with dated probe notes |
| Shared platform §2 (provenance, policy/audit, doubles, pollers) | M (spread) | Built incrementally inside phases, not as a big bang |
| **Total, A→C (locks the Apple + WhatsApp + Gmail core)** | **~4–6 weeks** | Highest owner value, lowest risk |
| **Total, A→F (everything)** | **~3–5 months** | D–F dominated by probes and vendor drift, not code volume |

Critical path: owner ceremonies (promotion, TCC grants, WhatsApp link,
OAuth consents, bot invites) — batch them per phase or the calendar, not
the code, becomes the bottleneck.

## 6. Unresolved (verbatim, from caller state)

### 6.0 Dated probe notes — Phase F (2026-10-11, agent session)

- **Meetings join (Teams/Zoom): agent-side foundation landed.** `normalize_event`
  now keeps `location`/`hangout_link`/`conference_links`, and pure
  `extract_meeting_links()` classifies teams/zoom/meet/webex/facetime/generic
  join URLs (`backend/app/digital/adapters/calendar.py`, 5/5 in
  `backend/tests/test_meeting_links.py`). No Zoom/Teams/Webex apps are
  installed on the Mac, so join execution stays future work: the designed path
  is extractor → show URL → owner-confirmed open via the approval-gated Mac
  control grant. In-meeting state (mute/leave verification) is unverified and
  must not be claimed until an app exists to probe against.
- **Messenger/Instagram DMs: PARKED (no silent partials).** No Meta tokens,
  no installed surfaces, and the plan's checkpoint-persistence + selector
  probes (§6 items 4–5) require a live logged-in session the owner has not
  provided. Do not build CDP automation here until: (a) a persistent,
  owner-logged-in browser profile exists, (b) checkpoint survival is probed
  over 7+ days, (c) selectors are pinned against the live DOM. Until then
  these lanes stay `unavailable` with this note, per the Phase F gate.
- **FaceTime audio join:** `facetime://` URLs classify in the extractor, but
  placing/answering calls was in the unrecovered g-sys group (not this plan's
  10 lanes) — needs its own probe wave against Continuity state on the Mac.

1. `iMessage lock-screen send/read; E2E send after buddy note; SMS-relay-off wording; group targeting; FSEvents vs poll`
2. `AppleMail live reply/forward verbs + Envelope Index read-flag column need FDA host probe`
3. `GmailWeb headless Google bot/CAPTCHA probe; owner Mail.app Gmail-config state`
4. `Telegram Desktop AX map; owner bot-alerts vs full user-account pref`
5. `Signal Desktop AX richness; stable compose entry (scheme/CLI) existence`
6. `Slack Electron AX depth; workspace admin/DLP policy; desktop-AX mark-read side-effects`
7. `Discord live AX walkability; bot server scope; discord:// deep-link channel nav caps`
8. `Meetings Teams/Zoom scriptable mute/join API; Zoom local-recording path`
9. `SocialDM Meta checkpoint persistence in headless; messenger.com/instagram.com selectors`
10. `g-sys: incomplete`
11. `g-plat: incomplete`
12. `critic: incomplete`
13. `WA companion-device limit/eviction handling (code + doc)`
14. `ChatStorage FDA grant doc (path + System Settings steps)`
15. `launchd supervisor plist + QR-expiry watcher for CDP workspace`

Notes on items 10–12: `g-sys`, `g-plat`, and `critic` arrived with no
bodies (see header). They are carried here verbatim and cannot be refined
until their content is delivered.

## 7. Sources read this session (grounding index)

- `docs/IPHONE_CAPABILITY_PLAN.md` — binding/sandbox ladder, camera/Mac-control
  diagnosis, Phase 1–4b status, invariants.
- `docs/IPHONE_PRODUCT.md` — PWA-primary product, trust states, owner unlock
  checklist.
- `docs/PERMISSIONED_OPERATING_LAYER.md` — risk classes, confirmation design,
  adapter/execution/memory/audit layers.
- `docs/POL_CAPABILITY_MATRIX.md`, `docs/POL_DEPENDENCIES_AND_LIMITS.md`,
  `docs/POL_IMPLEMENTATION_ROADMAP.md` — readiness labels, limits, phase order.
- `docs/INTEGRATIONS.md` (§§3–4 vault/OAuth, §15 WAVE LIFE helper contract +
  buddy DEPENDENCY NOTE, §16 WhatsApp CDP workspace + QR ceremony).
- `docs/GATEWAY.md` (§3 life tools + autonomy modes).
- `backend/app/integrations/life_helper.py:1-98` — wire contract, exit codes,
  delivery vs compose commands.
- `macos/Sources/EVLifeHelper/main.swift:318-399` (messages.send),
  `:401-436` (whatsapp.send), `:587-641` (mail.list/send),
  `:961ff` (chat.db), `:1010-1083` (Envelope Index, inbox-only SELECT).
- `backend/app/ev/messaging/whatsapp_local.py:1-75` — read-only cache,
  schema pin, freshness.
- `backend/app/ev/messaging/whatsapp_cdp.py:67-75,180-245,365-402` —
  link markers, linked/QR diagnosis, preflight states.
- `backend/app/digital/adapters/whatsapp.py` (FakeWhatsAppBacking,
  BackgroundWhatsAppBacking ranges per IPHONE_CAPABILITY_PLAN; verified
  `whatsapp.*` act verbs this session).
- `backend/app/digital/adapters/gmail.py:1-60` — native Gmail scopes +
  availability.
- `backend/app/device_gateway/phone_mac.py:19-67` — phone-home capabilities
  + blocked set + observe exemption.
- `backend/app/config.py:749-757` — `life_helper_path`,
  `messaging_provider`, helper timeout/output caps.
- `backend/app/digital/classify.py:15`, `extract.py:21` — only Teams/Zoom
  mentions are meeting-word regexes (lane is greenfield).
- `launchd/` listing — existing plist family the supervisor plist joins.
- `backend/app/digital/adapters/` listing — only browser, calendar,
  contacts, files, gmail, phone, whatsapp exist; telegram/signal/slack/
  discord/messenger/instagram lanes are greenfield.
- `docs/WORKSPACE_ANALYSIS.md` (header/method only, for tree context).

## 8. Conflicts and stale-doc candidates found while grounding

1. **Prior results missing** (header): plan cannot reconcile with g-sys /
   g-plat / critic until bodies arrive.
2. **Gmail send path**: `gmail.py` native write scopes vs INTEGRATIONS §10
   "send stays on the headless Mail.app helper" — Phase C must pick one
   and update the loser doc.
3. **Buddy-targeting fix status**: INTEGRATIONS §15.2 DEPENDENCY NOTE vs
   current `main.swift:334-363` still using `buddy … of targetService` —
   Phase A acceptance decides empirically.
4. **WhatsApp CDP envs**: `EV_WHATSAPP_CDP_PROFILE`/`EV_WHATSAPP_CDP_PORT`
   (INTEGRATIONS §16) did not surface in the `config.py` grep this session —
   verify whether they live in `whatsapp_cdp.py`/env-fallback or need
   Settings fields before documenting them as `EV_*`.
5. **FSEvents**: no watcher found in the grepped surface; §2.8 defaults to
   poll-first until a probe lands.
