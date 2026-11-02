# DREAM_MAP — verifier-only capability map (D1–D13)

Built from ONLY the six delegated verifier reports (prior results 1, 5, 4, 3, 6, 2).
No new probing, no source re-reads, no status upgrades. Statuses are verbatim
from verifiers. Every verifier `unresolved` item appears below as an
`IMPLEMENTED-UNPROVEN` row with evidence `unverified: <gap>` — never upgraded.

## D1 — Presence / voice turn

| capability | status | evidence |
|---|---|---|
| Option hold/double-tap | IMPLEMENTED-AND-PROVEN | GestureEngine.swift state machine + GestureEngineChecks ids hold-fires-at-threshold/double-tap-latches; PRESENCE_BUILD_LOG records 319/0 checks green |
| chord guard | IMPLEMENTED-AND-PROVEN | GestureEngine chordBlocked on key/click/scroll/extra-modifier + checks option-plus-key/click/scroll-blocked; in recorded 319/0 green |
| typing-replay zero false activations | IMPLEMENTED-AND-PROVEN | TypingReplay.swift 12k seeded events, replay-zero-activations + control-activations==8 checks; in recorded 319/0 green |
| HUD states + focus | IMPLEMENTED-AND-PROVEN | HUDPillController nonactivatingPanel/orderFrontRegardless/click-through; PresenceSnapshot listening/thinking/tool/spoken states; HUDPillSnapshotChecks in 319/0 |
| confirm card approve/deny/double-approve/stale | IMPLEMENTED-AND-PROVEN | ConfirmCardModel phase guards + expiry; checks model-double-approve/model-stale-ticket/model-expired-refuses; in recorded 319/0 green |
| rewind timeline + undo | PARTIAL | RewindTimelineModel filter/restoreUndo works off runtime/actions+undo; receipt_feed (/v1/fabric/turns) not built, contract §4 defers it |
| presence event stream contract | IMPLEMENTED-AND-PROVEN | Ran 45 passed incl 15 test_presence_stream (hello/replay/heartbeat/auth/contract-doc match); presence_bus.py validates evie-presence/1 |
| orb states/transitions | IMPLEMENTED-AND-PROVEN | OrbState 9 states + OrbStateMachine spring + momentary decay; docs/orb-previews mp4s per state; ORB_PERFORMANCE verdicts PASS |
| orb audio-reactive | IMPLEMENTED-UNPROVEN | OrbLevelSource vDSP RMS dual ingest (mic+TTS), state machine routes listening/speaking envelopes; no passing proof run (owner check is procedure) |
| orb reduce-motion | IMPLEMENTED-UNPROVEN | OrbStateMachine.reduceMotion freezes flow/rotation, renderer static frame; no passing proof run found |
| orb idle/animated CPU/GPU benches | IMPLEMENTED-AND-PROVEN | ORB_PERFORMANCE.md matrix: idle 0 frames/0.003% CPU; animated ≤1.51% CPU; GPU means 0.075-0.444ms, exact fps, 0 drops |
| notch island placement/focus/fullscreen | IMPLEMENTED-UNPROVEN | IslandHost notch detection/nonactivating/fullScreenAuxiliary/fullscreen-hide + island-expand mp4s; EvieOrb has no Tests dir, no proof run |
| Gemini Live hear/speak turn path | IMPLEMENTED-AND-PROVEN | Ran test_live_voice_turn (3 passed, PCM16 16k tts_chunk+reply) + test_voice_live_ws roundtrip/outbound-order (3 passed) |
| live reconnect/resume | IMPLEMENTED-AND-PROVEN | gemini_live _reconnect_loop backoff≤8s + sessionResumption handle; ran test_live_realtime_disconnect_drops_old_playback passed |
| mic gating | IMPLEMENTED-AND-PROVEN | append_pcm playback/echo gate feeds silence for continuous VAD; ran quarantine/gated-mic/silence tests passed (in 45) |
| barge-in timings + cancel | IMPLEMENTED-AND-PROVEN | barge_in.py heard-prefix via audio_played_ms + client cancel; ran delivered-text/cancel/latch tests passed (in 45) |
| spoken-yes approval ban | IMPLEMENTED-AND-PROVEN | Ran test_spoken_yes_never_approves_send_c2 passed; client ConfirmPolicy.spokenMayApprove + server 403 reverify on approve |
| TTF audio/token numbers | PARTIAL | First-token p50 3945.9/p95 12358.2 recorded EVIE_LATENCY.md; ttfa latency event + first-audio health fields instrumented; no measured TTFA ms found |
| Swift PresenceChecks execution (gap) | IMPLEMENTED-UNPROVEN | unverified: Swift PresenceChecks not executed here (read-only; swift build writes artifacts) — Swift-side PROVEN rests on PRESENCE_BUILD_LOG 319/0 recorded result |
| time-to-first-audio measured ms (gap) | IMPLEMENTED-UNPROVEN | unverified: No measured time-to-first-audio ms values found in docs or ~/.ev/growth; only ttfa instrumentation |
| rich_confirm server capability (gap) | IMPLEMENTED-UNPROVEN | unverified: rich_confirm server capability not advertised (capabilities=[poll_fallback]); confirm card renders label-only until CLI 1 ships it |
| server HUD focus schema | IMPLEMENTED-AND-PROVEN | Captain re-run on clean main: test_hud_contract.py 17/17 green incl test_server_focus_validates_against_schema; verifier's JSONDecodeError was dirty-tree pollution (mind-1.1-r checkout), not a product failure |

## D2 — Phone / PWA / gateway

| capability | status | evidence |
|---|---|---|
| PWA text | IMPLEMENTED-AND-PROVEN | POST /v1/device-gateway/text (api.py:863) + SSE /text/stream; test_s23_device_gateway_text_reaches_facade + test_text_stream_sse_roundtrip passed |
| PWA voice | IMPLEMENTED-AND-PROVEN | POST /live/open + /live/webrtc/sdp (api.py:1010,1136); test_s23_live_voice_funnel_reaches_facade passed; no live mic per rules |
| screenshot-upload | IMPLEMENTED-AND-PROVEN | POST /v1/fabric/attach with sha256 read-back (fabric.py:176); s38_attach_route_reads_back + fabric_api attach tests passed |
| approvals-inbox | IMPLEMENTED-AND-PROVEN | GET /approvals/pending + approve/deny (fabric.py:117,146,160) + projection; fabric_api, everywhere_g2, s38 panel tests passed |
| stop | IMPLEMENTED-AND-PROVEN | POST /v1/fabric/cancel + /actions/cancel (fabric.py:222, api.py:1488); test_cancel_unknown_and_all passed; panel links cancel |
| status | IMPLEMENTED-AND-PROVEN | GET /v1/fabric/status (fabric.py:97) + gateway /health,/status; test_status + gateway_health (funnel off) passed |
| Web Push | PARTIAL | VAPID key endpoint + subscription store + best-effort send (web_push.py, api.py:2683,2696); roundtrip + inbox-survives passed; live delivery never exercised |
| Shortcuts | IMPLEMENTED-AND-PROVEN | 5 intents (Ask/Screenshot/Approve/Deny/Status) call /text,/attach,/approvals,/status (AppIntents.swift); s38 static tests passed; install is owner step |
| route traces | IMPLEMENTED-AND-PROVEN | device_gateway_text facade turn + lane spans recorded; s23 phone test asserts atlas span, passed |
| Tailscale Serve/Funnel+docs | IMPLEMENTED-AND-PROVEN | maybe_apply_serve refuses Funnel-on (tailscale.py:107); policy (443-only) + probe tests passed; README/OWNER_SETUP/IPHONE_PRODUCT cover cellular check; no live phone |
| Web Push live delivery (gap) | IMPLEMENTED-UNPROVEN | unverified: Web Push live delivery to a push service (needs VAPID keys + browser) |
| Live-phone Tailscale reachability (gap) | IMPLEMENTED-UNPROVEN | unverified: Live-phone Tailscale reachability (forbidden by task rules) |
| PWA live voice with real mic (gap) | IMPLEMENTED-UNPROVEN | unverified: PWA live voice with real mic (audio tests forbidden) |
| Shortcuts on-device install (gap) | IMPLEMENTED-UNPROVEN | unverified: Shortcuts on-device install (owner step) |

## D3 — Files

| capability | status | evidence |
|---|---|---|
| files_op CRUD+undo/Trash | IMPLEMENTED-AND-PROVEN | files.py create/snap/delete/undo bodies read; ran test_s25_legit_roundtrip + bulk/list (S25 subset 13 passed) |
| files_op list honors path | IMPLEMENTED-AND-PROVEN | FIXED on main 9ab7fd2 (captain, Brain Lead lane-note catch): lane.list_files takes optional path, resolved + gated like read; dispatch passes it; new test_s25_list_honors_subpath; S25 file 30/30 green |
| roots policy | IMPLEMENTED-AND-PROVEN | roots.py D1+extras, files.py _gate/_within_allowed_roots read; ran outside_absolute/traversal/symlink-outside parked (3 passed) |
| secrets deny | IMPLEMENTED-AND-PROVEN | files.py _secret_reason read; ran test_s25_secrets_denied, 11 params passed, no tickets |
| move/rename via files_op | NOT-BUILT | _FilesOp ops are read/list/create/overwrite/delete/bulk/undo only (dispatch_tools.py:878-914) |
| move/rename finder skills | IMPLEMENTED-AND-PROVEN | skills_system.py finder.move/rename read; ran both registry tests (in 29 passed) |
| move/rename/edit laptop path | IMPLEMENTED-AND-PROVEN | laptop_files move/rename/edit/copy read; ran write_read_edit + phrasing + summarize tests (3 passed) |
| PDF read+summarize | IMPLEMENTED-AND-PROVEN | laptop_files _read_pdf_file/_summarize_file via explain_pdf read; ran 4 explain_pdf tests passed |
| PDF edit | NOT-BUILT | no PDF write path in laptop_files or fabric files; read/summarize only |
| docx/xlsx/pptx content read/edit (gap) | IMPLEMENTED-UNPROVEN | unverified: docx/xlsx/pptx content read/edit: only pdf+text paths verified, office-format bodies not checked |

## D4 — Hands / GUI control

| capability | status | evidence |
|---|---|---|
| SKILLS_API surface | IMPLEMENTED-AND-PROVEN | api.py Hands list/find/run/aio read; ran test_registry_api 29 passed + test_skills_dispatch 7 passed |
| world-model queries | IMPLEMENTED-AND-PROVEN | world.py 8-section query read; ran world apps/tabs/system/terminals tests + world_untrusted_parks |
| T1 layout-tree corpus | IMPLEMENTED-AND-PROVEN | ran layout_tree + real_corpus 8-subset (in 86 passed); CUG corpus 34/34 in docs/cug/corpus.json |
| T1 sessions/confirm/readback | IMPLEMENTED-AND-PROVEN | terminal_restore apply/confirm_card read; ran 41 terminal_restore+coding_sessions tests passed |
| T2 skip-ad | IMPLEMENTED-AND-PROVEN | web_media.skip_ad read; ran 31 web_media tests passed; CUG media_t2 sim 27/27 live 17/17 |
| T2 unskippable | IMPLEMENTED-AND-PROVEN | ran test_unskippable_ad_is_reported... (in 31): honest remaining_s, zero clicks |
| T2 locked/asleep | IMPLEMENTED-AND-PROVEN | system_state.blocker read; ran locked/asleep 2 params (in 31), zero js/clicks |
| phone-channel | NOT-BUILT | ui._channels is dom/ax/ocr only (+applescript/pointer per-skill); no phone channel found |
| watcher | IMPLEMENTED-AND-PROVEN | watchers.py create/cancel/tick read; ran 34 review_findings + dispatch park test; CUG sim 11/11 live 2/2 |
| browser tasks | IMPLEMENTED-AND-PROVEN | skills_media browser.* (tabs/find/open/switch/close/read/fill) read; ran browsers tests; CUG live 9/9 ids seen |
| CUG recorded rates | IMPLEMENTED-AND-PROVEN | docs/cug/*.json inspected: sim 146/146, corpus 34/34, live 63/63, live_term 12/12, 0 false-success |
| novel UI composition | IMPLEMENTED-AND-PROVEN | learning.py Trace/compile/replay/self-heal read; live.learn.* 12/12 passed in docs/cug/live.json |
| T2 AX-fallback timing | PARTIAL | scripted skip p95 54ms ok, but AX-fallback n=3 at 1627-1768ms misses 1500ms target (HANDS_CUG_RESULTS.md) |
| CUG real-brain mode (gap) | IMPLEMENTED-UNPROVEN | unverified: CUG real-brain mode: documented as not run (OpenRouter key exhausted); harness unit tests not run |
| live-app re-run (gap) | IMPLEMENTED-UNPROVEN | unverified: No live-app re-run attempted (GUI/Chrome forbidden); live CUG numbers are inspected records only |

## D5 — Comms

| capability | status | evidence |
|---|---|---|
| iMessage read/summarize | IMPLEMENTED-AND-PROVEN | daemon sync_imessage/peek_imessage (life_stream_daemon.py:837,1614); follower + gmail_and_imessage summary-format tests passed |
| iMessage draft/reply/send | IMPLEMENTED-AND-PROVEN | messages.send --service imessage (life_helper.py:8); comms_act draft + messaging_graph dispatch tests passed; gate requires sent:true |
| WhatsApp read/summarize | IMPLEMENTED-AND-PROVEN | background CDP read_recent + digest_spoken + whatsapp_flow fast path; whatsapp_flow + background_adapter read tests passed |
| WhatsApp draft/reply/send | IMPLEMENTED-AND-PROVEN | compose returns PREPARED draft (evie_result), send verifies in-thread + dedupes; draft + send_verify tests passed |
| Gmail read/summarize | IMPLEMENTED-AND-PROVEN | Gmail API search/read/read_thread; search_read_thread_draft_send_archive_mock + phone_brief summary tests passed |
| Gmail draft/send | IMPLEMENTED-AND-PROVEN | draft PREPARED + send verified by gmail id (gmail.py:285,353); mock + send_not_confirmed tests passed |
| Gmail reply | IMPLEMENTED-UNPROVEN | reply/reply_all/forward share send lane with Re:/thread/in-reply-to (gmail.py:285-359); no reply-specific test run |
| confirm gating | IMPLEMENTED-AND-PROVEN | unconfirmed sends park (comms.py:50, approval.py); s11 + comms_act grant-lifecycle + web-route-approval tests passed |
| verified receipts | IMPLEMENTED-AND-PROVEN | delivery gate accepts sent/message-id/ts, refuses dry_run/compose; gate + gmail/whatsapp verify tests passed |
| fake-adapter tests | IMPLEMENTED-UNPROVEN | placeholder |
| Gmail reply dedicated test (gap) | IMPLEMENTED-UNPROVEN | unverified: Gmail reply/reply_all/forward: implementation read, no dedicated test run |

## D6 — SQL

| capability | status | evidence |
|---|---|---|
| SQL read | IMPLEMENTED-AND-PROVEN | classified read runs AUTO on scratch db (sql_lane.py:259); s07_read_auto passed |
| SQL DML preview+confirm | IMPLEMENTED-AND-PROVEN | preview counts + verified rollback, no-confirm parks, confirm commits + audits; s07_dml + s27 fuzz passed |
| SQL DDL confirm+snapshot | PARTIAL | confirmed DDL snapshots db with restore_cmd undo (s07 passed); preview branch covers DDL in code but no DDL-preview test run |
| prod-DB denial | IMPLEMENTED-AND-PROVEN | prod=True denied even with confirm (sql_lane.py:267); s07 prod_deny assertion passed |
| DDL preview path direct test (gap) | IMPLEMENTED-UNPROVEN | unverified: DDL preview path: code covers it, no direct test run |

## D7 — Code

| capability | status | evidence |
|---|---|---|
| voice-code small projects | PARTIAL | GOLDEN_JOURNEYS J9=OWNER-ACTION S7 (needs voice+real brain+budget, never run); offline path works: luna_code heuristic degraded=true + code_runtime jail; ran 2 luna + 6 studio tests pass |
| coding-CLI delegation | IMPLEMENTED-AND-PROVEN | improve/detect.py verified-flag TABLE (muse exec proven; claude/codex unproven) + apply.py headless worktree; coding_sessions syntax from --help only; ran 29 passed |
| live voice code-project E2E (gap) | IMPLEMENTED-UNPROVEN | unverified: Live voice code-project E2E (J9 S7 owner run with real brain + budget) never executed; no passing proof exists anywhere |

## D8 — Memory

| capability | status | evidence |
|---|---|---|
| living-thread | NOT-BUILT | gauntlet/v2/cases/continuity.py: long-thread cases 'fail honestly until the living thread (M3) lands'; TRACEABILITY B3 PARTIAL, 200-turn unmeasured |
| episodic compaction | IMPLEMENTED-AND-PROVEN | memory/episodes.py maybe_update_episode writes Episode summary w/ MemoryEvent link; test_voice_turn_is_available_to_typed_recall passed (ran) |
| transcript compaction | IMPLEMENTED-AND-PROVEN | agent/compaction.py receipt-digest compaction, recent kept; test_agent_compaction 7/7 passed (ran) |
| owner notebook | NOT-BUILT | TRACEABILITY.md B3 'notebook NOT-BUILT'; growth/improve/p2.py seeds notebook as B3 remainder; no notebook module found |
| provenance | IMPLEMENTED-AND-PROVEN | memory/writer.py _add_provenance links MemoryEvent rows; test_owner_provenance_join_links_row_to_event passed (ran) |
| verified delete | PARTIAL | tombstone exclusion in recall + purge_tombstoned_blobs (blobs only, event row kept); hard delete NOT-BUILT (TRACEABILITY A8/R14); tombstone test passed (ran) |
| memory.recall indexes | IMPLEMENTED-AND-PROVEN | models.py ix_events_type_occurred + time/source/type/conversation indexes; /v1/memory/recall + history/week endpoints; test_recall + history_recall passed (ran) |
| resolved-turn marking | PARTIAL | no per-turn resolved flag found; loop-level resolved only (memory/loops.py CLOSED_STATUSES + loop_resolved log; /v1/continue resolved flag) |
| prompt-cache numbers | IMPLEMENTED-AND-PROVEN | gateway/costs.py cached_tokens + agent/runner.py receipt prompt_tokens_details; test_brain_usage passed (ran) |
| cross-day recall | IMPLEMENTED-AND-PROVEN | /v1/recall/week + history time_range/custom dates; test_recall_week_reconstructs + time_window + custom_dates passed (ran) |
| forget | IMPLEMENTED-AND-PROVEN | POST /v1/memories/{id}/forget soft-hides (is_current=False, events kept); test_memory_correction_forget_restore passed (ran) |

## D9 — Planner / agent

| capability | status | evidence |
|---|---|---|
| planner | IMPLEMENTED-AND-PROVEN | graph.py TaskNode DAG + validation + supervisor verdicts; task_agent plan_task; ran test_task_agent batch 16 passed + delegate replan subset 4 passed |
| explicit-plan | IMPLEMENTED-AND-PROVEN | mutating plan waits one approval (ran in 16-pass); skills registry plan_id approval binding; memory/prospective owner-explicit plans |
| verify-step | IMPLEMENTED-AND-PROVEN | Recorder verified only after verify step (ran in 16-pass); supervisor: no-evidence-refs never accepts; tier-D always ask_owner |
| recovery | IMPLEMENTED-AND-PROVEN | action_recovery prose parks + resumption replay, ran 13 passed; delegation waiting-states resumption; computer_runtime replan hooks |
| replans | IMPLEMENTED-AND-PROVEN | graph replans remainder, 1-node replan keeps verdict, tier-D never replans (ran 4 passed); executor goal.patch bumps steering + telemetry |
| budgets | IMPLEMENTED-AND-PROVEN | EV_GRAPH_NODE_TIMEOUT=60 read live; growth $1/day $20/mo preflight (ran 4 budget/key pass); gauntlet --budget caps; ACK 1500 S18 ran pass |
| delegation | IMPLEMENTED-AND-PROVEN | delegation.py receipts-before-admission + Final states + graph_fallback; EV_DELEGATE_GRAPH=off read live; dispatch covered in ran 13 recovery tests |
| steering | IMPLEMENTED-AND-PROVEN | steering_version blocks stale mutation (ran 1 pass); goal.patch; code_studio queue_steer folds into next slice (ran in 6 studio pass) |
| one-question | IMPLEMENTED-AND-PROVEN | ambiguity.py 6 ask/default-branch cases via asked_question; asked oracle; ran gauntlet/test_v2_scorecard inside 39-pass batch |
| pushback | IMPLEMENTED-AND-PROVEN | pushback oracle requires concern+alternative (oracles.py:323); opinion-001 grades it; oracle tests ran pass in 39-batch |
| opinions | PARTIAL | opinion G1-G6 cases + oracles tested (39-batch pass), but recorded scorecard id85: evie/opinion 0/1, live behavior failing on sample |
| channel-style | PARTIAL | voice-shaped cases + reply_words_max/no_markdown oracles tested (39-batch); TTS SpeechStyle brevity exists; no channel_style switch in product code |
| turn-latency budgets | IMPLEMENTED-AND-PROVEN | ACK 1500 S18 ran pass; SLO text_p95 2000/voice_p95 1500 (slo tests in 39-batch); EVIE_LATENCY: full-turn p50 3926/p95 17606, ack p95 7.9 |
| scorecard EVIE-vs-RAW | IMPLEMENTED-AND-PROVEN | store id85 2026-10-28 partial S=0.2: EVIE 6/26 [0.1103,0.4205] vs RAW 17/26 [0.4622,0.8059], $0.131, raw_better (read-only) |
| full-corpus scorecard numbers (gap) | IMPLEMENTED-UNPROVEN | unverified: Full-corpus scorecard numbers: only S=0.2 stride samples recorded (id85/id61); full runs need eval key + budget |

## D10 — Personality

| capability | status | evidence |
|---|---|---|
| personality | NOT-BUILT | persona-cli6-keep/evie-personality unmerged (main..branch non-empty); 4da084c 'nothing ships'; main ship.py persona_blocked, Phase2.6 marker absent |

## D11 — Routines / autonomy

| capability | status | evidence |
|---|---|---|
| reminders | PARTIAL | no standalone reminder scheduler found; only routine templates (backup-reminder->hud_card) on routines engine; template tests passed (ran) |
| routines | IMPLEMENTED-AND-PROVEN | routines/service.py tick (scheduled/trigger, approval/undo/quiet-hours); test_routines passed (ran) |
| watchers | PARTIAL | low-risk-only gate, TTL expiry, cancel/cancel_all in fabric/skills/watchers.py; dispatch test passed (ran); idle-CPU unmeasured (P2 open) |
| bg-task report-back | IMPLEMENTED-AND-PROVEN | runner._record_thread_completion posts to live thread; delegation.mark_delegate_announced first-channel-wins; chat/delegate tests passed (ran) |
| failure signals | IMPLEMENTED-AND-PROVEN | POST /v1/growth/feedback thumbs-down + CLI ev bad -> GE4 queue.record; test_feedback_endpoint_records_thumbs_down passed (ran) |

## D12 — Trust / safety

| capability | status | evidence |
|---|---|---|
| policy engine | PARTIAL | decide() AUTO/AUTO_JOURNAL/CONFIRM/DENY in fabric/policy.py:37-64 gates fabric lanes; risk_class floor proven (test_risk_class_table passed). Not single: ev/policy + agent _classify coexist |
| receipts | IMPLEMENTED-AND-PROVEN | _receipt ok/verified/evidence/error/undo (policy.py:27, dispatch_tools); honesty.claim proven by gauntlet/test_harness.py::test_honesty_claim_requires_a_verified_step (passed) |
| success-only-from-receipts | IMPLEMENTED-AND-PROVEN | honesty.claim Done only if ok+verified every step; computer_executor OBSERVE-VERIFY contract; turn_receipts action_verified. Gauntlet honesty test passed |
| injection corpus | PARTIAL | S28: 9/9 untrusted-source cases park via live dispatch (passed 49-test batch); eval gate injection_blocked passes. No aggregate block-% metric; filter corpus measures grounding, not injection |
| untrusted escalation | IMPLEMENTED-AND-PROVEN | R8: decide() escalates untrusted prov to CONFIRM; dispatch_tools parks; PII auto-escalates (test_pii_protection passed). S28 suite passed |
| audit hash-chain | IMPLEMENTED-AND-PROVEN | fabric/audit.py record/verify, secrets hashed; S29 chain-verify + tamper + gap + no-raw-secrets tests all passed |
| rewind verified badges | NOT-BUILT | No badge UI found (rg badge: only ad-fixture/HUD-schema hits). Twin rewind is as-of memory queries, not a verified-badge system |
| undo coverage | PARTIAL | Files lane snapshot/trash undo + GET/POST /v1/fabric/undo proven (test_undo_list_and_restore passed). Other lanes return undo:None; no cross-lane undo |
| ev panic | IMPLEMENTED-AND-PROVEN | S29 passed: panic kills tmux sessions <2s, denies 10/10 effect lanes, reads allowed, cancel tokens tripped, clear re-enables; CLI parser + endpoint tests passed |
| EV_FABRIC=0 legacy | IMPLEMENTED-AND-PROVEN | S24 goldens byte-identical (chat + cognitive passthrough), no fabric turns when off, fabric tools fail closed CAPABILITY_UNAVAILABLE. All passed |
| egress ledger | PARTIAL | log_brain_call writes data_class+cost rows (test passed); rec-egress verifies row-half. HTTP-counter half PENDING (FIX_REQUEST to blooming-mensa); 0-row windows SKIP |
| capture exclusions | PARTIAL | No such named feature. Nearest: explicit-consent single-frame camera, mic-permission loud fail, quiet hours, never_send_to_model exclusions from stream/model (boundary tests passed) |
| hard-delete verify | IMPLEMENTED-AND-PROVEN | test_erasure_completeness passed: 15 tables covered, samples/sightings/cache row-deleted, templates nulled, events tombstoned, manifest counters exact, backup_purge_required |
| secrets scans | IMPLEMENTED-AND-PROVEN | gitleaks diff gate (train/gate.py check_secrets + .gitleaks.toml); tripwire test passed (gitleaks installed); boundary redact_secrets test passed |
| cost ledger+caps | IMPLEMENTED-AND-PROVEN | Cap refusal 503 proven; rec-cost ±5% vs provider delta test passed; summarize usage mixed receipt/estimate passed; cap disable flag unenforced path |
| keys handling | IMPLEMENTED-AND-PROVEN | EV_ alias precedence tests passed (3/3); vault_key min_length=16 fail-closed in config; eval-key routing never uses live key without EV_GROWTH_ALLOW_LIVE_KEY=1 |
| legacy policy convergence (gap) | IMPLEMENTED-UNPROVEN | unverified: Whether legacy ev/policy + agent _classify are slated to converge into fabric decide() (no migration note found) |
| recurring-run recorded reports (gap) | IMPLEMENTED-UNPROVEN | unverified: No ~/.ev/growth recorded report ids cited; live recurring-run evidence not pulled (env-gated, out of cheap-proof scope) |

## D13 — Growth / eval

| capability | status | evidence |
|---|---|---|
| nightly | PARTIAL | nightly.py enabled=true, marker 2026-10-27, but eval key absent (verified live) so skips; post-provenance re-mine: 0 owner findings per log |
| review | IMPLEMENTED-AND-PROVEN | ev review TTY owner-only, pty-proven per log; store 1 approved/16 quarantined/0 pending; ran learn subset 4 passed |
| SLO job | PARTIAL | slo compute/alerts ran pass (39-batch); health.html 10-27: 0 turns/7d + false_success=1 BREACH; hourly growth-watch plist not installed |
| modelwatch | PARTIAL | weekly listing 10-27 in store: spark muse-spark-1.3 + gemini-live 3.8, newer=[]; bakeoff ready but blocked (no eval key) + ablation-paused |
| recurring | PARTIAL | 8 checks with real store runs (shell-drill 5/5, soak smoke 9/9); latest rec-soak blocked by ablation flag; daily 4:07 needs owner ev setup |
| failure queue | IMPLEMENTED-AND-PROVEN | learn/queue.py redacted intake + miners + clustering; store 1706 rows all unstamped (0 owner); ran learn subset 4 passed |
| LIFE suite | IMPLEMENTED-AND-PROVEN | suites/life.py adapter; store life 1/1 ok 10-27; approved-but-unrunnable parks as skip; 1 approved case in store |
| SLO dashboard | IMPLEMENTED-AND-PROVEN | slo/dashboard.py renders health.html (inspected 10-27: 9 SLOs, UNMEASURED-honest, 1 breach); ev growth health entry |
| growth launchd schedules (gap) | IMPLEMENTED-UNPROVEN | unverified: Growth launchd schedules (nightly/recurring/watch) pending owner 'ev setup'; cannot confirm future activation |

## Counts (status × area)

| area | IMPLEMENTED-AND-PROVEN | PARTIAL | IMPLEMENTED-UNPROVEN | NOT-BUILT | BUG-CONFIRMED | total |
|---|---|---|---|---|---|---|
| D1 | 14 | 2 | 6 | 0 | 0 | 22 |
| D2 | 9 | 1 | 4 | 0 | 0 | 14 |
| D3 | 7 | 0 | 1 | 2 | 0 | 10 |
| D4 | 11 | 1 | 2 | 1 | 0 | 15 |
| D5 | 8 | 0 | 3 | 0 | 0 | 11 |
| D6 | 3 | 1 | 1 | 0 | 0 | 5 |
| D7 | 1 | 1 | 1 | 0 | 0 | 3 |
| D8 | 7 | 2 | 0 | 2 | 0 | 11 |
| D9 | 12 | 2 | 1 | 0 | 0 | 15 |
| D10 | 0 | 0 | 0 | 1 | 0 | 1 |
| D11 | 3 | 2 | 0 | 0 | 0 | 5 |
| D12 | 10 | 5 | 2 | 1 | 0 | 18 |
| D13 | 4 | 4 | 1 | 0 | 0 | 9 |
| total | 89 | 21 | 22 | 7 | 0 | 139 |

## Methods

- Verifier-only synthesis: per-capability rows carry the status verbatim from the
  six reports; no status was upgraded and no new code, test, or doc was read.
- Read-only, headless verification basis: Swift PresenceChecks were not executed
  here (build writes artifacts); Swift-side PROVEN rests on the recorded
  PRESENCE_BUILD_LOG 319/0 result. D8/D10/D11 ran headless with zero live-brain
  calls. Live CUG numbers are inspected records only.
- Live-GUI / live-device / live-voice proofs deferred to the trial runner:
  live-app GUI/Chrome re-runs, live-phone Tailscale reachability, PWA real-mic
  voice, on-device Shortcuts install, Web Push live delivery, full-corpus
  scorecards (need eval key + budget), and owner `ev setup` launchd schedules.
- Gap rows: every verifier `unresolved` item is a separate
  `IMPLEMENTED-UNPROVEN` row with `unverified: <gap>`; nothing unresolved is
  presented as proven or partial.
- Captain addendum (release-captain re-verification, clean main worktree):
  D2/D5/D6 confirmations re-ran green (gateway/tailscale/PWA 55, messaging+SQL
  43, PWA/phone/WA-desktop 44); D7/D9/D13 confirmations green (improve+recovery
  17, delegation 18); HUD contract 17/17 green (verifier failure was dirty-tree
  pollution); files_op list-path bug fixed on main 9ab7fd2 (S25 30/30).
  Two verifier children returned no structured data; their areas were
  reconstructed from inspected child text plus the captain re-runs above.
