# INTEGRATION GAPS — persona needs from other lanes

Status after §2.6 (2026-10-28): GAP-4 APPLIED on mind-1.1-r
(commits 33977b2 + 96e57c1). GAP-1/2/3/5/6 diffs staged on
branch `persona/gaps` (one commit per file + index). GAP-7
remains owner work (no hook).

Each entry: file, line, exact diff, benefit. CLI 6 does not edit these
files; owners apply. (Phase 1 handoffs in `personality/handoffs/` carry
the same requests in DEPENDENCY NOTE form.)

## GAP-1. [ON persona/gaps] Kernel identity line (works today, speaks v4-voice, not v2.1)

File: `backend/app/cognitive/context.py`, line 98. Not the default path
(default `/v1/chat` serves the legacy pipeline; verified in PHASE1.md).

```diff
@@ backend/app/cognitive/context.py @@
     persona = (getattr(settings, "persona_name", None) or "EVIE").strip() or "EVIE"
     blocks = [
-        f"You are {persona}, the owner's one mind. Speak naturally. Do not mention tools, graphs, or orchestration unless asked.",
+        _persona_block_or_fallback(persona),
```

Benefit: kernel path (api-first profile, `EV_COGNITIVE_MODE=mimo_kernel`)
speaks persona.v2.1 with TURN TONE instead of a fixed one-liner. Fallback
keeps today's line when the flag is off.

## GAP-2. [ON persona/gaps] Real-path reply hook (closes the learning loop)

File: `backend/app/api/core.py`, after the reply is final (near the
`run_chat_pipeline` return, ~line 2330). One call:

```diff
@@ backend/app/api/core.py @@
     reply_text = result["reply"]
+    if PERSONA_ENABLED:  # from app.persona.settings (cheap import)
+        from app.persona.live import after_decision as _persona_after
+        # _persona_after(store, key, state, decision, reply_text) — needs the
+        # ToneDecision kept from prompt build; see live.begin_turn/note_turn.
+        pass  # owner wires: decision + reply + key are all in scope here
     return result
```

Benefit: landed-bit capture, stance filing, and no-repeat lines on the
real path. Today those run only in the lab (delegation sees no reply
text). Latch decay + reaction/weight updates already work without this.

## GAP-3. [ON persona/gaps] NOW-signal plumbing (receipts, task, calendar into guidance)

File: `backend/app/ev/turn.py`, lines 151 and 246 (`turn_guidance(message)`
/ `turn_guidance(working_on)`). `build_system_prompt` holds receipts,
working_on, context, and briefing but `turn_guidance` receives only a
string.

```diff
@@ backend/app/ev/turn.py @@
-        parts.append(turn_guidance(working_on))
+        parts.append(turn_guidance(working_on, receipts=receipts))
```

plus an accepted `receipts: list = []` kwarg on `turn_guidance` (my file;
I will add it once the caller passes it). Benefit: rule-1 (failed receipt
→ ROAST 0) and receipt-aware NOW lines on the real path. Today the
delegation infers failure only from message text.

## GAP-4. [APPLIED (33977b2 + 96e57c1)] Filter guard stage (PersonaGuard on the text path)

File: `backend/app/filter/output_filter.py`, `enforce_persona`, after
guardrails, before the length trim:

```diff
@@ backend/app/filter/output_filter.py @@
     text = apply_persona_guardrails(text, ...)
+    if PERSONA_ENABLED:
+        from app.persona.guard import persona_guard
+        text, persona_flags = persona_guard(text, None)
+        flags.extend(persona_flags)
     text = trim_to_length(text, ...)
```

Benefit: cliché strips, emoji caps, repeat/on-behalf flags on live text
replies. Note: pass a PersonaContext when the filter owns one; None keeps
today's behavior plus the static checks.

## GAP-5. [ON persona/gaps] Voice instruction builders (flag-aware systemInstruction)

Files: `backend/app/voice/live/gemini_live.py:818`
(`gemini_live_instructions`), `backend/app/device_gateway/webrtc_live.py:369`
(`phone_webrtc_session`). Both embed `SPEECH_STYLE_INSTRUCTIONS`, so they
already get the v2.1 CORE under a boot-time flag at no cost. The gap is
only the dynamic slice (TURN TONE / NOW / exemplars), which voice never
had:

```python
# in gemini_live_instructions(), after the base instruction is built:
if PERSONA_ENABLED:
    from app.persona.delegation import delegated_turn_block
    instruction += "\n" + delegated_turn_block(transcript_text or "")
```

Benefit: voice gets per-turn tone ceilings (roast caps, serious latch)
instead of the static core only. Live voice has no post-filter, so this
is prompt-side only by design.

## GAP-6. [ON persona/gaps] Shared config fields (formalize the module constants)

Files: `backend/app/config.py`, `.env.example`, `docs/ENVIRONMENT.md`.
Append a CLI-6-marked block (request text in
`personality/handoffs/05-shared-config-fields.md`):
`EV_PERSONA_ENABLED=false`, `EV_PERSONA_PROFANITY=false`,
`EV_PERSONA_NIGHT_START=00:30`, `EV_PERSONA_NUDGE_GAP_MIN=90`,
`EV_PERSONA_MAX_NUDGES=2`, `EV_PERSONA_WAKE_TIME=`,
`EV_PERSONA_BRIEF_MODE`, `EV_PERSONA_NIGHT_SCOPE`,
`EV_PERSONA_JOKE_RATE_MIN/MAX`. Benefit: operator-visible flag + tuning
without code edits; until then `app/persona/settings.py` holds the same
defaults.

## GAP-7. [OWNER WORK (no hook)] Agent-lane night execution + scheduling (request text exists)

`run_night_shift(plan)` entry point (`app/agent/loop.py` owner) and
morning-brief delivery (scheduling owner). Full specs in
`personality/handoffs/07-agent-night-shift.md` and `08-...`. Benefit:
unlocks the Night Shift honesty gate (`NIGHT_SHIFT_EXECUTION_AVAILABLE`).
Until then the full flow runs in the lab only, by design.

## Mission-end status (2026-10-28, gate FAIL — flag OFF)

No change from the §2.6 state above: GAP-4 applied and green (flag-off
identity covered by `test_gap4_*`); GAP-1/2/3/5/6 still staged on
`persona/gaps` with reasons in `PERSONA_GAPS_BRANCH.md`; GAP-7 still
owner work. Nothing was re-attempted after the eval rounds (no tuning
remained, and the gate failed on reply quality, not on integration).
Next iteration should re-run the §2.6 collision checks before
cherry-picking — the <48h reasons may have cleared.
