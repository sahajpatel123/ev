"""Listener Presence is CANCELLED (owner decision 2026-08-23).

Invariants after removal from the active product:
- the ``listener_presence`` control is accepted-and-ignored (old clients
  must not break, and it can never affect audio state again);
- the server engine never produces backchannel cues, regardless of any
  flag, so no "Mhm."/"Yeah."/"Okay." listener speech can ever be scheduled;
- one speech authority only: the normal assistant response.
"""

from __future__ import annotations

import asyncio

from app.voice.live.backchannel import BackchannelDecision
from app.voice.live.engine import LiveEngine
from app.voice.live.session import LiveSession


def _session() -> LiveSession:
    return LiveSession(session_id="listener-gate", engine=LiveEngine())


def test_listener_presence_control_is_inert() -> None:
    session = _session()
    asyncio.run(
        session._handle_control(
            "listener_presence",
            {"type": "control", "action": "listener_presence", "v": 1},
        )
    )
    # The control must not mutate any audio-affecting state.
    assert session._backchannel_task is None


def test_engine_never_produces_backchannel_cues() -> None:
    engine = LiveEngine()
    # Even with the legacy attribute left True (quarantined), ticks during
    # owner speech must yield no backchannel cue.
    engine.backchannel_enabled = True
    engine.push_transcript("the owner is talking at length here")
    engine.state.user_is_speaking = True
    tick = engine.commit()
    assert tick.backchannel is None


def test_legacy_tick_backchannel_is_never_scheduled_as_speech() -> None:
    session = _session()
    # A tick carrying a legacy backchannel payload must be ignored: no
    # speech task may exist afterwards.
    from app.voice.live.engine import TURN_KEEP_LISTENING, EngineTick, TurnDecision

    decision = TurnDecision(action=TURN_KEEP_LISTENING)
    tick = EngineTick(
        decision=decision,
        events=[],
        backchannel=BackchannelDecision(cue="Mhm."),
    )
    asyncio.run(session._apply_tick(tick))
    assert session._backchannel_task is None
