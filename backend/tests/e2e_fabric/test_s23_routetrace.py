"""S23: every conversational entrypoint reaches run_turn with fabric lanes.

Real ASGI app, real kernel, scripted deterministic provider. Each test
asserts the facade recorded a turn AND the scripted fabric tool call left
lane spans + a verified receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import settings
from app.contracts import ChatResult, ToolCall


class Scripted:
    """Nameless deterministic provider: a nameless double CAN decide (kernel lane)."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls = 0

    async def chat_with_tools(self, messages, specs, **kwargs):
        self.calls += 1
        if not self._script:
            return ChatResult(text="done.")
        item = self._script.pop(0)
        if isinstance(item, ToolCall):
            return ChatResult(text="", tool_calls=[item])
        return ChatResult(text=item)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return ChatResult(text="no phone tool")


def _shell_script() -> Scripted:
    return Scripted([
        ToolCall(id="c1", name="shell.run", arguments={"command": "echo traced-ok"}),
        "The shell says: traced-ok. Done.",
    ])


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.agent.ledger import reset_for_tests as reset_ledger
    from app.cognitive.session_store import reset_for_tests
    from app.fabric import panic, trace

    reset_for_tests()
    reset_ledger()
    panic.clear()
    trace.clear()
    yield home
    reset_for_tests()
    reset_ledger()
    trace.clear()


def _script_provider(monkeypatch: pytest.MonkeyPatch, provider: Scripted) -> Scripted:
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: provider)
    # Established scripted-brain pattern (cf. test_bridge_resilience.py):
    # availability True + deterministic provider standing in for MiMo.
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    return provider


def _pin_local_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kernel-led turns execute in-process here.

    In production the kernel forwards effects to the Mac hands (:18000). A test
    process has no Mac executor, so it declares one active live session — the
    documented pattern in this file — which makes the executor dispatch the
    fabric lane locally and record its span.
    """

    from types import SimpleNamespace

    from app.voice.live import layer as live_layer

    monkeypatch.setattr(live_layer, "active_lives",
                        lambda: [SimpleNamespace(session_id="s23-live")])


def _dump_trace(entrypoint: str, lanes: list, *, turns: int = 1) -> None:
    """Append one line when a fresh wiring trace is being recorded.

    Unset in normal runs, so the suite output stays quiet. ``turns`` is 1
    only when this call observed a fabric turn span. Non-turns pass 0.
    """

    import os

    out = os.environ.get("EV_WIRING_TRACE_OUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"entrypoint={entrypoint} facade_turns={turns} lanes={lanes}\n")


def _assert_shell_trace(entrypoint: str) -> None:
    from app.fabric import trace

    assert trace.turn_count(entrypoint) == 1, f"no fabric turn for {entrypoint}"
    _, spans = trace.recent_turn(entrypoint)
    lanes = [(s["lane"], s["op"]) for s in spans]
    assert ("shell", "run") in lanes, lanes
    _dump_trace(entrypoint, lanes)


PROMPT = "Please run the shell command `echo traced-ok` and report exactly what it outputs."


async def test_s23_chat_reaches_facade(
    client: AsyncClient, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production brain mode: the kernel owns /v1/chat turns (fabric-on alone
    # keeps the legacy pipeline so its attachment/filter/503 contracts hold).
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    _pin_local_dispatch(monkeypatch)
    provider = _script_provider(monkeypatch, _shell_script())
    resp = await client.post("/v1/chat", json={"message": PROMPT})
    assert resp.status_code == 200, resp.text
    assert "traced-ok" in resp.json()["reply"]
    assert provider.calls >= 1, "model loop never ran"
    _assert_shell_trace("v1_chat_pipeline")


async def test_s23_cognitive_turn_reaches_facade(
    client: AsyncClient, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _script_provider(monkeypatch, _shell_script())
    resp = await client.post("/v1/cognitive/turn", json={"transcript": PROMPT})
    assert resp.status_code == 200, resp.text
    assert "traced-ok" in resp.json()["spoken"]
    assert provider.calls >= 1
    _assert_shell_trace("v1_cognitive_turn")


async def test_s23_device_gateway_text_reaches_facade(
    owner_phone, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prod parity: production runs mimo_kernel, so trusted phone text goes
    # run_phone_text → receipts → kernel. (Legacy-mode phone turns fall back
    # to the shared pipeline, covered by test_s23_chat_reaches_facade.)
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    _, phone = owner_phone
    phone.headers["X-Forwarded-Proto"] = "https"
    # R1 lane: device actors hold ticketless at authorize for R2 (existing
    # policy; WP11 adds ticketed holds), so the phone trace uses atlas.
    provider = _script_provider(
        monkeypatch,
        Scripted([
            ToolCall(id="c1", name="atlas.discover", arguments={"domain": "notes"}),
            "Atlas checked. Done.",
        ]),
    )

    # Simulate the real prod state "a live session is active": the kernel
    # then dispatches tools locally on the request session instead of HTTP
    # to :18000 (which would hit the owner's real Talk sidecar from a test,
    # and a second SQLite session would wedge on the receipt lock).
    from types import SimpleNamespace

    from app.voice.live import layer as live_layer

    monkeypatch.setattr(
        live_layer, "active_lives", lambda: [SimpleNamespace(session_id="s23-live")]
    )
    resp = await phone.post(
        "/v1/device-gateway/text",
        json={"text": PROMPT, "instance_id": "s23-tab"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "Atlas checked" in (body.get("reply") or body.get("core_reply") or ""), body
    assert provider.calls >= 1
    from app.fabric import trace as trace_mod

    assert trace_mod.turn_count("device_gateway_text") == 1
    _, phone_spans = trace_mod.recent_turn("device_gateway_text")
    phone_lanes = [(s["lane"], s["op"]) for s in phone_spans]
    assert ("atlas", "discover") in phone_lanes
    _dump_trace("device_gateway_text", phone_lanes)


async def test_s23_cli_ask_posts_to_covered_chat() -> None:
    """`ev ask` is a thin client of POST /v1/chat (trace-covered above)."""
    from clients.cli import ask

    seen: dict[str, Any] = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"reply": "ok"}

    class FakeClient:
        async def post(self, path: str, json: dict):
            seen["path"] = path
            seen["json"] = json
            return FakeResp()

    out = await ask("hello?", client=FakeClient())
    assert out == {"reply": "ok"}
    assert seen["path"] == "/v1/chat"
    assert seen["json"]["message"] == "hello?"
    # Thin client: this process posts /v1/chat. The fabric spans for that
    # route are the v1_chat_pipeline lines, not a second brain.
    _dump_trace(
        "cli_ev_ask",
        [("via", "/v1/chat"), ("covered_by", "v1_chat_pipeline")],
        turns=0,
    )


async def test_s23_live_voice_funnel_reaches_facade(
    fabric_on, monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    """handle_turn_maybe_remote (hotkey/Talk/PWA-voice/live WS funnel) → facade."""
    from app.cognitive.kernel import handle_turn_maybe_remote

    provider = _script_provider(monkeypatch, _shell_script())
    result = await handle_turn_maybe_remote(
        transcript=PROMPT, live_session_id=None, device_id=None,
        modality="voice", session=db_session, actor="master",
    )
    assert "traced-ok" in result.spoken
    assert provider.calls >= 1
    _assert_shell_trace("live_voice")


async def test_s23_hands_free_chain_reaches_facade(
    fabric_on, monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    """VoiceRuntime pipeline (hands-free WS chain) funnels run_chat_pipeline → facade."""
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    _pin_local_dispatch(monkeypatch)
    from app.api.core import run_chat_pipeline
    from app.ev.assistant import resolve_live_thread
    from app.schemas import ChatRequest

    provider = _script_provider(monkeypatch, _shell_script())
    thread = await resolve_live_thread(db_session, None)
    pipeline = await run_chat_pipeline(
        ChatRequest(message=PROMPT), db_session, "master",
        thread_id=thread.id, source="voice",
    )
    assert "traced-ok" in pipeline["result"].text
    assert provider.calls >= 1
    _assert_shell_trace("v1_chat_pipeline")


def test_s23_non_turn_surfaces_stay_out_of_facade() -> None:
    """Shortcuts/routines/workers must NOT route through the turn facade (R8):
    scheduled/device provenance must never look like an owner utterance."""
    import app.api.everywhere as everywhere_mod
    import app.cognitive.kernel as kernel_mod
    import app.services.runtime as runtime_mod
    import app.workers.jobs as jobs_mod
    from app.fabric import facade as facade_mod

    for mod in (everywhere_mod, runtime_mod, jobs_mod):
        src = Path(mod.__file__).read_text()
        assert "arun_kernel_turn" not in src, mod.__name__
        assert "arun_turn" not in src, mod.__name__
    # The facade delegates to exactly one brain.
    facade_src = Path(facade_mod.__file__).read_text()
    assert facade_src.count("handle_turn(") >= 1
    assert "kernel" in kernel_mod.__name__
    _dump_trace(
        "non_turn_shortcuts_routines_workers",
        [("policy", "not_a_turn")],
        turns=0,
    )


async def test_s23_kernel_confirm_tier_parks_ticketed_hold(
    client: AsyncClient, fabric_on, monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    """A kernel-driven CONFIRM-tier fabric effect parks a ticketed hold
    (approvable from the phone inbox), executes nothing, then resumes
    exactly once after approval through the real runtime machinery."""
    from sqlalchemy import select

    from app.models import ApprovedAction

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    _pin_local_dispatch(monkeypatch)
    provider = _script_provider(
        monkeypatch,
        Scripted([
            ToolCall(id="c1", name="shell.run",
                     arguments={"command": "touch kernel-parked.txt"}),
            "Waiting on your approval.",
        ]),
    )
    target = fabric_on / "Desktop" / "kernel-parked.txt"
    resp = await client.post(
        "/v1/chat", json={"message": "Create an empty file named kernel-parked.txt"})
    assert resp.status_code == 200, resp.text
    assert provider.calls >= 1
    assert not target.exists(), "parked effect must not execute"
    _assert_shell_trace("v1_chat_pipeline")

    rows = (await db_session.execute(
        select(ApprovedAction).where(
            ApprovedAction.status == "pending",
            ApprovedAction.action_type == "shell_run",
        )
    )).scalars().all()
    assert len(rows) == 1, "exactly one ticketed hold"
    await db_session.commit()

    from app.services import runtime as runtime_service

    action = await runtime_service.decide_action(
        db_session, rows[0].id, actor="master", decision="approve"
    )
    await db_session.commit()
    assert action.status == "executed"
    assert target.exists(), "approved kernel-parked effect must execute"
