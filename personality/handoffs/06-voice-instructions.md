# DEPENDENCY NOTE 06 — to the voice owner (from CLI 6 low-telescopium)

## Build live-voice instructions with build_persona_block (flag-gated)

Files: `backend/app/voice/live/gemini_live.py:818`
(`gemini_live_instructions`) and
`backend/app/device_gateway/webrtc_live.py:369` (`phone_webrtc_session`).

Request: construct the `systemInstruction` text with
`build_persona_block(ctx)` from `app.persona.compile`, passing
`channel="voice"` (Mac) or `channel="phone"` as appropriate, behind the
`PERSONA_ENABLED` flag. Keep your existing transport/tool-routing text;
the persona block replaces only the persona portion (today:
`SPEECH_STYLE_INSTRUCTIONS` + surrounding style lines). Flag off =
byte-identical instructions.

Note: live voice has no post-filter — `persona_guard` cannot run there —
so keep the voice roast cap (2) and zero-emoji rule in the assembled
prompt; both are already in the v2 core + TURN TONE line.

Acceptance: with the flag off, setup payloads are byte-identical; with the
flag on, `systemInstruction` contains the v2 core + a TURN TONE line and
no `[text]` clauses.
