# Evie Deprecations — Canonical Modules

| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Ears (mic owner) | ears mic owner (single mic claimant) | ad-hoc mic opens, parallel listeners, background recorders outside ears |
| Shell + PWA | EvieShell + PWA | legacy web shells, duplicate launchers, standalone debug pages |
| Kernel loop | kernel loop (sole runtime loop) | secondary tickers, ad-hoc schedulers, competing run loops |

Rule: route new mic/audio work through ears, new UI surface through EvieShell+PWA,
new periodic work through the kernel loop. Remove callers of deprecated paths on touch.

## Fabric lanes (2026-10-17, branch evie-fabric)
| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Files | `backend/app/fabric/files.py` | direct `open()/shutil` writes outside policy roots, silent overwrites |
| Shell | `backend/app/fabric/shell.py` (+ `shell_sessions.py`) | ad-hoc `subprocess` calls bypassing policy/grant checks |
| SQL scratch lane | `backend/app/fabric/sql_lane.py` | raw sqlite writes outside scratch lane, multi-statement exec |
| Screen refs | `backend/app/fabric/screen.py` + `screen_ref.py` | raw pixel dumps without redaction refs |
| Bridge/iPhone | `backend/app/fabric/bridge.py` | unverified device claims, relay without receipts |
| Comms draft/send | `backend/app/fabric/comms.py` | sending without draft→review→send gate |
| Plan/apply builder | `backend/app/fabric/builder.py` | apply without plan diff + confirm |
| Turn orchestration | `backend/app/fabric/run_turn.py` + `turn.py` | calling lanes directly from routes, skipping receipts |
| Policy/grants/gaps | `backend/app/fabric/policy.py`, `grants.py`, `gaps.py` | hard-coded limits, implicit grants, silent gaps |
| Terminal layouts | `backend/app/fabric/terminal.py` (TmuxLayout) + `ghostty.py` (scripted Ghostty 1.3.1 sdef: windows/tabs/splits/input/read-back, live-probed WP4) | ad-hoc tmux strings without plan/confirm/readback |

## Voice stacks (WP10, D5)
| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Mic owner | `backend/clients/ears/` (single claimant; ev.ears launchd) | `backend/clients/hands_free/main.py`, `backend/clients/device_listener.py` (refuse to start unless `EV_VOICE_LEGACY_STACKS=true`) |
| Wake/ASR/TTS weights | `ev models install` (`app/voice/models_setup.py`) | hand-rolled downloads without checksums |

## Phone + code-by-voice (WP9/WP11)
| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Phone panel | `backend/clients/pwa/evie-phone.js` on `/evie` (approvals, upload, stop, status) | per-feature ad-hoc debug pages, duplicated approval UIs |
| Phone attachments | `POST /v1/fabric/attach` (read-back verified) + `attachment_ids` on the text/stream turn | sending image bytes inline in a transcript, trusting client OCR text |
| Typed turn taint | kernel `untrusted_context` → executor `provenance=untrusted_content` (R8 CONFIRM) | letting a model claim its own provenance, clearing taint inside a turn |
| Coding jobs | `backend/app/fabric/builder.py` (`code.act` `engine=builder`) | ad-hoc long agent invocations outside the job store/fix loop |
| Speech weights | `ev models status/install` | silently downloading weights at request time |

## Models (2026-10-25, two-model workspace)
| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Brain (all non-speech reasoning) | `backend/app/gateway/openrouter_muse.py` (`MuseSparkProvider`, `BrainUnavailable`, `BrainEgressDenied`); id in `EV_BRAIN_MODEL` | any other chat provider, including generic OpenRouter and local chat providers |
| Voice | `backend/app/voice/live/gemini_live.py`; id in `EV_GEMINI_LIVE_MODEL` | any other speech model or REST `generateContent` model |
| Supervisor verdicts | `backend/app/gateway/decider.py` over `decide_via_role` (categorical) | any separate verdict model, model-reported probabilities |
| Model-id checks | `ev models verify` | trusting a configured id without reading the provider listing |
| Muse via Meta Model API | none: Muse Spark is reached through OpenRouter only | direct vendor APIs and other routes |

