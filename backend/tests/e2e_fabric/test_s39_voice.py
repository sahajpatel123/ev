"""S39: wake -> turn -> speak with doubles (no mic, no weights)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_s39_menu_bar_pgrep_matches_evie_and_the_shim() -> None:
    import re

    from clients.ears.main import MENU_BAR_PGREP

    pat = re.compile(MENU_BAR_PGREP)
    assert pat.search("/Applications/Evie.app/Contents/MacOS/EV")
    assert pat.search("/Applications/EV.app/Contents/MacOS/EV")
    assert not pat.search("/Applications/Notes.app/Contents/MacOS/EV")


def test_s39_wake_phrase_strict_name() -> None:
    from clients.ears.wake import WAKE_TOKEN, normalize

    assert WAKE_TOKEN.search(normalize("hey evie, what time is it"))
    assert WAKE_TOKEN.search(normalize("EVE HERE"))
    assert not WAKE_TOKEN.search(normalize("every evening stevie"))
    assert not WAKE_TOKEN.search(normalize("even steven"))


async def test_s39_wake_turn_speak_doubles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated wake event -> kernel turn (scripted) -> TTS sink (fake)."""
    from app.config import settings
    from app.fabric import facade, panic, trace
    from clients.ears.wake import WAKE_TOKEN, normalize

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    panic.clear()
    trace.clear()

    transcript = "hey evie, say hello back"
    assert WAKE_TOKEN.search(normalize(transcript)), "wake word must match"

    async def scripted(**kwargs):
        from app.cognitive.kernel import KernelResult

        return KernelResult(spoken="Hello back.", kind="mimo")

    monkeypatch.setattr("app.fabric.facade.handle_turn", scripted)
    spoken_log: list[str] = []

    async def fake_speak(text: str) -> dict:
        spoken_log.append(text)
        return {"ok": True, "bytes": len(text)}

    out = await facade.arun_turn(transcript, entrypoint="voice_wake", session=None)
    assert out["ok"] is True
    await fake_speak(out["spoken"])
    assert spoken_log == ["Hello back."]
    assert trace.turn_count("voice_wake") == 1


async def test_s39_stop_cancels_voice_turn(tmp_path: Path) -> None:
    """Barge-in/stop: a cancelled token aborts the turn before effects."""
    from app.fabric.run_turn import run_turn
    from app.fabric.turn import CancelToken

    token = CancelToken()
    token.cancel()  # owner said "stop" before the turn started
    out = run_turn("do file work", root=tmp_path,
                   ops=[{"lane": "files", "op": "create",
                         "args": {"path": "nope.txt", "content": "x"}}],
                   cancel=token)
    assert out["ok"] is False
    assert not (tmp_path / "nope.txt").exists()


def test_s39_mic_probe_honest_shape() -> None:
    """The mic probe lists devices without opening audio (fast, safe)."""
    from clients.cli.models import _mic_status

    status = _mic_status()
    assert isinstance(status.get("reachable"), bool)
    assert isinstance(status.get("devices"), list)
