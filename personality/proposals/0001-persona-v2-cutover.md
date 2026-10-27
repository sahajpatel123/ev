# 0001 — persona.v2 cutover (WRITE ONLY — do not apply)

Status: PROPOSAL. Applies only after the owner records the FLEET_LAW §13
override scoped to the character layer. CLI 6 executes part A; parts C and
D are executed by their owners. Nobody applies anything before the override.

## A. SPEECH_STYLE_INSTRUCTIONS delegates to persona.core (CLI 6)

File: `backend/app/ev/personality.py` (owner-frozen until the override).

Rollback preserved: flag off (or any persona import failure) keeps the
current frozen string byte-identical. The existing `EV_PERSONALITY_DYNAMIC`
rollback is untouched.

```diff
@@ backend/app/ev/personality.py @@
 from sqlalchemy import select
 from sqlalchemy.ext.asyncio import AsyncSession

 from app.models import PersonalityProfile
 from app.schemas import PersonalityUpdate

+# --- CLI 6 (persona.v2 cutover, behind flag; rollback = delete this block) ---
+def _persona_v2_core() -> str | None:
+    """persona.v2 core text, or None when the flag is off / unavailable."""
+    try:
+        from app.persona import settings as persona_settings
+        from app.persona.core import core_text
+    except Exception:
+        return None
+    if not persona_settings.PERSONA_ENABLED:
+        return None
+    try:
+        return core_text("text")
+    except Exception:
+        return None
+_PERSONA_V2_CORE = _persona_v2_core()
+# --- end CLI 6 ---
+
 # This is intentionally a code-owned contract rather than a setting. Feature
 # work may add tools and context, but it must not silently retune EV's voice.
 PERSONALITY_CONTRACT_VERSION = "dynamic.v4"
```

and at the single use site that surfaces the law (`speech_contract_suffix`,
`identity_block` compact/full paths, and every `SPEECH_STYLE_INSTRUCTIONS`
reference — all flow through the module constant):

```diff
 SPEECH_STYLE_INSTRUCTIONS = (
     "EV SPEECH CONTRACT — dynamic.v4; ..."
     ...
 )
+# Persona.v2 delegation (flag-gated; None keeps dynamic.v4 byte-identical).
+if _PERSONA_V2_CORE is not None:
+    SPEECH_STYLE_INSTRUCTIONS = _PERSONA_V2_CORE
+    PERSONALITY_CONTRACT_VERSION = "persona.v2"
```

Effect: with the flag off, zero behavior change (the lock tests below pin
this). With the flag on, every existing surface (legacy text, filter,
Gemini Live, phone WebRTC) speaks the v2 core through the one function
`build_persona_block` once callers pass their context — the constant swap
is step one; per-surface `build_persona_block(ctx)` wiring follows per the
handoffs in `personality/handoffs/`.

## B. Lock tests' deliberate-update path (CLI 6, same change)

File: `backend/tests/test_personality_lock.py` (fleet-owned; CLI 6 edits only
the pinned persona strings, nothing else, after the override).

Which pinned strings change and why:

- `test_speech_contract_reaches_every_speech_surface` (line 40): with the
  flag on, surfaces end with the v2 core, not the v4 string. Update: assert
  the prompt ends with `persona.core.core_text(channel)` when
  `PERSONA_ENABLED`, else the current assertion. The "EV SPEECH CONTRACT"
  marker assertion stays (v2 core keeps an "EVIE — CHARACTER CORE" header;
  the test gains the v2 header as an accepted alternative).
- `test_contract_covers_banned_filler_and_commentary` (line 46): the v2
  core keeps "as an AI" and the no-filler rule but drops the literal
  phrases "let me check" / "does that help" / "here is what I found" /
  "great question" (v2 bans them via the guard + never-list instead of
  in-prompt enumeration). Update: check those four against
  `app.persona.guard`'s banned set rather than the contract string.
- `test_contract_enforces_nothing_else_neutral_topics` (line 57):
  "NOTHING ELSE", "no closing offers", "no steering toward any feature",
  "background machinery" do not appear verbatim in v2 (v2's OPERATIONAL
  LINES + INITIATIVE cover the same ground: "Offer concretely what you'll
  do", "No filler openers or closers", no-flattery rule). Update: assert
  the v2 equivalents ("OPERATIONAL LINES", "No filler openers or closers",
  "Offer concretely") when the flag is on.
- `test_capability_questions_exempt_from_topic_neutrality` (line 67):
  "CAPABILITY QUESTIONS ARE THE EXCEPTION" / "past two sentences" are v4
  phrasing. v2 has no capability-steering exemption text (capability
  answers come from the operator sheet, not persona). Update: scope this
  test to flag-off; add a v2 assertion that the core contains no
  capability list.
- `test_memory_context_does_not_define_a_second_personality` (line 79):
  unchanged in spirit; the `.endswith(SPEECH_STYLE_INSTRUCTIONS)` assertion
  keeps passing because the constant itself delegates (part A).
- `test_default_profile_fully_pinned` (line 104), mutation tests (line 118):
  unchanged — sliders and owner-only mutation survive cutover.
- `tests/test_dynamic_personality.py`, `tests/test_personality_style.py`:
  same treatment — pinned v4 strings gain flag-conditional v2 branches;
  no test is deleted or weakened.

Rule: every changed assertion keeps its flag-off branch byte-identical, so
`PERSONA_ENABLED=False` runs the old suite exactly.

## C. Retire the competing kernel identity (kernel owner executes)

File: `backend/app/cognitive/context.py`, lines 96–98. Current text:

```python
    persona = (getattr(settings, "persona_name", None) or "EVIE").strip() or "EVIE"
    blocks = [
        f"You are {persona}, the owner's one mind. Speak naturally. Do not mention tools, graphs, or orchestration unless asked.",
```

Exact change (kernel owner applies; CLI 6 provides the interface only):

```diff
@@ backend/app/cognitive/context.py @@
     persona = (getattr(settings, "persona_name", None) or "EVIE").strip() or "EVIE"
     blocks = [
-        f"You are {persona}, the owner's one mind. Speak naturally. Do not mention tools, graphs, or orchestration unless asked.",
+        _persona_block_or_fallback(persona),
```

where the helper (added by the kernel owner near `compile_context`) is:

```python
def _persona_block_or_fallback(persona: str) -> str:
    try:
        from app.persona import settings as persona_settings
        if not persona_settings.PERSONA_ENABLED:
            raise ImportError("persona flag off")
        from app.persona.compile import build_persona_block
        from app.persona.context import PersonaContext
        # ... build ctx from compile_context() arguments (modality, device,
        # cognition, memories) and return build_persona_block(ctx).text ...
    except Exception:
        pass
    return (
        f"You are {persona}, the owner's one mind. Speak naturally. "
        "Do not mention tools, graphs, or orchestration unless asked."
    )
```

Fallback keeps today's behavior byte-identical when the flag is off. Full
ctx wiring (NOW-line signals, receipts, latch) follows handoff 02.

## D. Contract-ordering fix for the life-agency block (gateway owner executes)

File: `backend/app/gateway/service.py`, `_with_life_agency_prompt`, lines
94–104. Current behavior appends the life-agency block AFTER the system
message content — i.e. after the speech contract — whenever life tools are
offered, breaking contract-last ordering.

Exact change (gateway owner applies):

```diff
@@ backend/app/gateway/service.py @@
     result = list(messages)
     for index in range(len(result) - 1, -1, -1):
         if result[index].role == "system":
             if block not in result[index].content:
                 existing = result[index]
+                # Contract-last: capability blocks precede the persona
+                # contract; they must never follow it.
+                content = existing.content
+                contract_at = content.find("EVIE — CHARACTER CORE")
+                if contract_at < 0:
+                    contract_at = content.find("EV SPEECH CONTRACT")
+                if contract_at >= 0:
+                    content = (
+                        content[:contract_at].rstrip()
+                        + "\n\n" + block + "\n\n"
+                        + content[contract_at:].lstrip()
+                    )
+                else:
+                    content = f"{existing.content}\n\n{block}"
                 result[index] = ChatMessage(
                     role="system",
-                    content=f"{existing.content}\n\n{block}",
+                    content=content,
                     name=existing.name,
                     media=list(existing.media),
                 )
             return result
```

No behavior change when no contract marker is present (old append path).
With either contract present, the agency block moves before it. A
regression test asserting `system.index(block) < system.index(contract)`
for both markers belongs with this change (gateway owner adds it).
