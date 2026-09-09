"""Cognitive OS V2 — Muse kernel, Mini coprocessor, no second mind."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.voice.live.grok_voice import grok_session_update, grok_voice_tools


def _kernel(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "laptop_files", False)


class _ScriptedMuse:
    def __init__(self, replies: list[ChatResult]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.messages: list[Any] = []

    async def chat_with_tools(self, messages, specs, *, model=None, temperature=0.7):
        del specs, model, temperature
        self.calls += 1
        self.messages = list(messages)
        if not self.replies:
            return ChatResult(text="Okay.")
        return self.replies.pop(0)


@pytest.fixture
def cognitive_isolation(monkeypatch, tmp_path):
    _kernel(monkeypatch)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from app.cognitive import telemetry
    from app.cognitive.session_store import reset_for_tests

    reset_for_tests()
    telemetry.reset_for_tests()
    yield
    reset_for_tests()
    telemetry.reset_for_tests()
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")


@pytest.mark.asyncio
async def test_reflex_stop_without_muse(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn
    from app.cognitive.session_store import current, save
    from app.cognitive.telemetry import snapshot

    row = current()
    row.semantic_objective = "organize PDFs"
    save(row)
    result = await handle_turn(transcript="Stop.")
    assert result.kind.startswith("reflex")
    assert "Stopped" in result.spoken or result.spoken == "Okay."
    assert snapshot()["deterministic_reflex_turns"] >= 1
    assert snapshot()["muse_turns"] == 0


@pytest.mark.asyncio
async def test_reflex_status_without_muse(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn
    from app.cognitive.session_store import current, save

    row = current()
    row.semantic_objective = "inspect the repo"
    row.focused_goal_id = "goal-test"
    save(row)
    result = await handle_turn(transcript="What are you doing?")
    assert result.kind == "reflex:status"
    assert "inspect the repo" in result.spoken


@pytest.mark.asyncio
async def test_conversation_skips_goal_contract(cognitive_isolation, monkeypatch, db_session: AsyncSession) -> None:
    from app.cognitive import kernel
    from app.cognitive.session_store import current

    muse = _ScriptedMuse([ChatResult(text="I'm well — glad you're here.")])
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: muse)

    result = await kernel.handle_turn(
        transcript="How are you?",
        modality="voice",
        session=db_session,
    )
    assert "well" in result.spoken.lower() or "glad" in result.spoken.lower()
    assert result.kind == "muse"
    assert current().focused_goal_id is None
    assert muse.calls == 1


@pytest.mark.asyncio
async def test_phrase_independent_digital_composition(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive import kernel
    from app.cognitive.telemetry import snapshot

    seen: dict[str, Any] = {}

    async def _digital(session, name, args, *, actor):
        seen["name"] = name
        seen["args"] = dict(args)
        return {
            "status": "ok",
            "service": args.get("service"),
            "operation": args.get("operation"),
            "payload": {"messages": [{"from": "Rahul", "text": "the number is 42"}]},
            "ok": True,
        }

    monkeypatch.setattr("app.digital.tools.handle_digital_tool", _digital)
    muse = _ScriptedMuse(
        [
            ChatResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="digital.act",
                        arguments={
                            "service": "gmail",
                            "operation": "search",
                            "args": {"query": "Rahul"},
                        },
                    )
                ],
            ),
            ChatResult(text="Rahul's last note says the number is 42."),
        ]
    )
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: muse)

    novel = (
        "Find the email Rahul sent, compare it with what he said on WhatsApp "
        "and put the final number in the project."
    )
    result = await kernel.handle_turn(transcript=novel, modality="voice", session=db_session)
    assert seen.get("name") == "digital_act"
    assert seen.get("args", {}).get("service") == "gmail"
    assert "42" in result.spoken
    assert snapshot()["muse_tool_calls"] >= 1
    assert snapshot()["legacy_general_model_calls"] == 0


@pytest.mark.asyncio
async def test_steering_blocks_stale_mutation(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import bump_steering, current
    from app.cognitive.telemetry import snapshot

    captured: dict[str, Any] = {}

    async def _run(*_a, **_k):
        captured["arguments"] = _a[2] if len(_a) > 2 else _k.get("arguments")
        return {"ok": True, "prepare_only": True, "spoken": "I will prepare the remaining changes."}

    monkeypatch.setattr("app.cognitive.executor._run_existing", _run)
    row = current()
    row.semantic_objective = "fix the tests"
    row.focused_goal_id = "steer-1"
    bump_steering(row, prepare_only=True)
    version = int(current().steering_version)
    blocked = await execute_semantic(
        db_session,
        "code.act",
        {"effect": "write files"},
        cognition=current(),
        actor="master",
        live_session_id=None,
        steering_seen=version - 1,
    )
    assert blocked.get("error") == "STALE_PLAN"
    assert snapshot()["stale_mutations_blocked"] >= 1
    prepare = await execute_semantic(
        db_session,
        "code.act",
        {"effect": "patch remaining issues"},
        cognition=current(),
        actor="master",
        live_session_id=None,
        steering_seen=int(current().steering_version),
    )
    assert prepare.get("prepare_only") is True or "PREPARE_ONLY" in str(
        prepare.get("spoken") or prepare.get("effect") or prepare
    )


@pytest.mark.asyncio
async def test_muse_unavailable_does_not_fallback(cognitive_isolation, monkeypatch) -> None:
    from app.cognitive import kernel
    from app.cognitive.telemetry import snapshot

    def _boom(*_a, **_k):
        raise AssertionError("legacy brain must not run")

    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: False)
    monkeypatch.setattr("app.gateway.providers.get_chat_provider", _boom)
    monkeypatch.setattr("app.voice.live.grok_voice.grok_voice_tools", _boom)

    result = await kernel.handle_turn(transcript="Plan a research pass on Postgres.")
    assert result.unavailable is True
    assert "won't guess" in result.spoken.lower() or "can't think" in result.spoken.lower()
    assert snapshot()["unavailable"] >= 1
    assert snapshot()["grok_turns"] == 0
    assert snapshot()["legacy_general_model_calls"] == 0


def test_mini_tools_empty_on_muse_kernel(cognitive_isolation) -> None:
    tools = grok_voice_tools(
        [
            {"name": "computer", "description": "x", "parameters": {"type": "object"}},
            {"name": "code", "description": "x", "parameters": {"type": "object"}},
            {"name": "search_web", "description": "x", "parameters": {"type": "object"}},
        ]
    )
    assert tools == []
    update = grok_session_update(provider="openai", function_tools=[{"name": "computer"}])
    assert update["session"]["tools"] == []
    assert update["session"]["tool_choice"] == "none"
    assert update["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
    assert "coprocessor" in update["session"]["instructions"].lower()


@pytest.mark.asyncio
async def test_intelligence_judge_skipped(cognitive_isolation, monkeypatch) -> None:
    from app.voice.live.grok_voice import GrokVoiceBridge

    monkeypatch.setattr(settings, "intelligence_layer", "spark")
    bridge = GrokVoiceBridge.__new__(GrokVoiceBridge)
    bridge._voice_health = {}
    out = await GrokVoiceBridge.intelligence_judge_review(bridge, "hi", "hello")
    assert out.get("skipped") == "disabled"


def test_session_survives_restart(cognitive_isolation) -> None:
    from app.cognitive.session_store import current, forget_live_cache, save

    row = current()
    row.focused_goal_id = "persist-1"
    row.semantic_objective = "research then update notes"
    row.steering_version = 3
    save(row)
    forget_live_cache()
    loaded = current()
    assert loaded.focused_goal_id == "persist-1"
    assert loaded.semantic_objective.startswith("research")
    assert loaded.steering_version == 3


@pytest.mark.asyncio
async def test_verification_failure_cannot_complete(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import current

    async def _failing(*_a, **_k):
        return {"ok": True, "verified": False, "error": "VERIFICATION_FAILED"}

    monkeypatch.setattr("app.cognitive.executor._run_existing", _failing)
    body = await execute_semantic(
        db_session,
        "computer.perform_effect",
        {"effect": "pretend success"},
        cognition=current(),
        actor="master",
        live_session_id=None,
        steering_seen=int(current().steering_version),
    )
    assert body.get("completed_verified") is False
    assert body.get("verified") is False


def test_context_compiler_parity(cognitive_isolation) -> None:
    from app.cognitive.context import compile_context
    from app.cognitive.session_store import current

    row = current()
    voice = compile_context(
        transcript="Where were we?",
        modality="voice",
        device_id="mac",
        cognition=row,
        memories=[{"text": "Evie uses Postgres."}],
    )
    typed = compile_context(
        transcript="Where were we?",
        modality="typed",
        device_id="mac",
        cognition=row,
        memories=[{"text": "Evie uses Postgres."}],
    )
    assert "Evie uses Postgres." in voice
    assert "Evie uses Postgres." in typed
    assert "CAPABILITIES" in voice
    assert "CAPABILITIES" in typed
    assert "digital.act" in voice or "gmail" in voice.lower()
    assert "WORK SHAPE" in voice
    assert "WORK SHAPE" in typed


HOLDING = (
    "Look at the thing I'm holding in my hand. I want you to look at it "
    "and tell me more info about this item I'm holding"
)


def test_spark_camera_tool_is_on_the_muse_bus(cognitive_isolation) -> None:
    from app.cognitive.capabilities import tool_specs

    specs = tool_specs()
    names = [spec.name for spec in specs]
    assert "look.capture" in names
    spec = next(item for item in specs if item.name == "look.capture")
    lowered = spec.description.lower()
    assert "holding" in lowered
    assert "memorize" in lowered or "keep" in lowered


def compile_context_camera_policy() -> str:
    from app.cognitive.context import compile_context
    from app.cognitive.session_store import current

    return compile_context(
        transcript=HOLDING,
        modality="voice",
        device_id="mac",
        cognition=current(),
    )


def test_context_tells_spark_to_look_before_speaking(cognitive_isolation) -> None:
    text = compile_context_camera_policy()
    assert "look.capture" in text
    assert "CAMERA" in text
    assert "memory.search" in text
    assert "never refuse a look" in text.lower()


@pytest.mark.asyncio
async def test_look_capture_maps_to_live_look(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import current

    captured: dict[str, Any] = {}

    async def _run(_session, name, arguments, **_kwargs):
        captured["name"] = name
        captured["arguments"] = dict(arguments or {})
        return {"ok": True, "spoken": "I see a glass bottle.", "verified": True}

    monkeypatch.setattr("app.cognitive.executor._run_existing", _run)
    body = await execute_semantic(
        db_session,
        "look.capture",
        {"prompt": HOLDING},
        cognition=current(),
        actor="master",
        live_session_id="live-look",
        steering_seen=int(current().steering_version),
    )
    assert captured["name"] == "look"
    assert captured["arguments"]["prompt"] == HOLDING
    assert captured["arguments"]["focus"] == "auto"
    assert body.get("ok") is True


@pytest.mark.asyncio
async def test_spark_hold_look_calls_camera_not_a_refusal(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive import kernel

    dispatched: list[tuple[str, dict[str, Any]]] = []

    async def _run(_session, name, arguments, **_kwargs):
        dispatched.append((str(name), dict(arguments or {})))
        return {"ok": True, "spoken": "I see a glass bottle.", "verified": True}

    monkeypatch.setattr("app.cognitive.executor._run_existing", _run)
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    replies = [
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(id="look1", name="look.capture", arguments={"prompt": HOLDING})
            ],
        ),
        ChatResult(text="It's a glass bottle with a label."),
    ]
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: _ScriptedMuse(replies),
    )
    result = await kernel.handle_turn(
        transcript=HOLDING, session=db_session, modality="voice"
    )
    assert dispatched and dispatched[0][0] == "look"
    assert "cannot" not in result.spoken.lower()
    assert "bottle" in result.spoken.lower()
    assert result.kind == "muse"


@pytest.mark.asyncio
async def test_keep_recall_uses_memory_search(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive import kernel

    asked: list[str] = []

    async def _exec(*_a, **kwargs):
        name = _a[1] if len(_a) > 1 else kwargs.get("name")
        asked.append(str(name))
        return {"ok": True, "spoken": "You asked me to keep the glass bottle.", "verified": True}

    monkeypatch.setattr("app.cognitive.kernel.execute_semantic", _exec)
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    query = "what did I ask you to memorize?"
    replies = [
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="m1", name="memory.search", arguments={"query": query})],
        ),
        ChatResult(text="You asked me to keep the glass bottle."),
    ]
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: _ScriptedMuse(replies),
    )
    result = await kernel.handle_turn(
        transcript=query, session=db_session, modality="voice"
    )
    assert "memory.search" in asked
    assert "glass bottle" in result.spoken.lower()


def test_capability_discovery_is_structural(cognitive_isolation) -> None:
    from app.cognitive.capabilities import public_descriptors

    rows = public_descriptors(domain="gmail")
    names = [str(item.get("name")) for item in rows]
    assert any("gmail" in n or "digital" in n for n in names)
    secret_keys = {"access_token", "refresh_token", "password", "cookie", "authorization", "api_key"}
    for item in rows:
        assert secret_keys.isdisjoint({str(k).lower() for k in item})


def test_legacy_mini_still_advertises_tools() -> None:
    assert settings.cognitive_mode == "legacy_mini"
    tools = grok_voice_tools(
        [{"name": "search_memory", "description": "x", "parameters": {"type": "object"}}],
        mode="supervised",
    )
    assert any(item.get("name") == "search_memory" for item in tools)


@pytest.mark.asyncio
async def test_shadow_replay_no_side_effects(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    """Replay representative owner turns against the kernel with tools stubbed."""

    from app.cognitive import kernel
    from app.cognitive.telemetry import snapshot

    effects: list[str] = []

    async def _exec(*_a, **kwargs):
        name = _a[1] if len(_a) > 1 else kwargs.get("name")
        effects.append(str(name))
        return {"ok": True, "shadow": True, "verified": True}

    monkeypatch.setattr("app.cognitive.kernel.execute_semantic", _exec)
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)

    turns = [
        ("How are you?", [ChatResult(text="Doing well.")]),
        (
            "Remember the Postgres decision?",
            [
                ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="m1", name="memory.search", arguments={"query": "Postgres"})],
                ),
                ChatResult(text="We picked Postgres for the event store."),
            ],
        ),
        (
            "Organize PDFs from this month into the Evie project.",
            [
                ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="f1", name="files.act", arguments={"effect": "organize PDFs"})],
                ),
                ChatResult(text="Prepared a file plan."),
            ],
        ),
        ("Stop.", []),
    ]
    for transcript, replies in turns:
        if replies:
            monkeypatch.setattr(
                "app.gateway.muse_spark.muse_spark_provider",
                lambda replies=replies: _ScriptedMuse(list(replies)),
            )
        result = await kernel.handle_turn(transcript=transcript, session=db_session, modality="voice")
        assert result.spoken
        assert result.kind != "unavailable"
    assert snapshot()["legacy_general_model_calls"] == 0
    assert snapshot()["grok_turns"] == 0
    assert "memory.search" in effects or snapshot()["deterministic_reflex_turns"] >= 1


def test_secrets_never_reach_muse() -> None:
    from app.cognitive.executor import dump_tool_json

    blob = dump_tool_json(
        {
            "ok": True,
            "access_token": "secret-token",
            "cookie": "wa=1",
            "payload": {"subject": "hello", "password": "nope"},
        }
    )
    assert "secret-token" not in blob
    assert "wa=1" not in blob
    assert "nope" not in blob
    assert "hello" in blob


class _CoprocessorRealtime:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        await self.incoming.put(None)


@pytest.mark.asyncio
async def test_coprocessor_connect_does_not_error_on_empty_tools(cognitive_isolation) -> None:
    from app.voice.live.events import ErrorEvent
    from app.voice.live.grok_voice import GrokVoiceBridge

    events: list[Any] = []
    fake = _CoprocessorRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="openai",
        approved_tool_specs=[
            {
                "name": "computer",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )
    await bridge.start()
    assert fake.sent[0]["session"]["tools"] == []
    assert fake.sent[0]["session"]["tool_choice"] == "none"
    assert bridge.supports_function_calls is False
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "realtime_no_tools" for event in events
    )
    bridge.close()


@pytest.mark.asyncio
async def test_coprocessor_ignores_mini_function_calls_and_keeps_mic_open(
    cognitive_isolation,
) -> None:
    import time

    from app.voice.live.events import ErrorEvent
    from app.voice.live.grok_voice import GrokVoiceBridge

    events: list[Any] = []
    fake = _CoprocessorRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="openai",
        approved_tool_specs=[
            {
                "name": "computer",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )
    await bridge.start()
    await bridge._handle_upstream({"type": "session.updated", "session": {"tools": []}})
    fake.sent.clear()
    await bridge._run_tool(
        {
            "name": "computer",
            "call_id": "call_coprocessor",
            "arguments": "{}",
        }
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert bridge._tool_gap_gate_until == 0.0 or bridge._tool_gap_gate_until <= time.monotonic()
    assert bridge._playback_blocks_mic() is False
    assert any(
        item.get("type") == "conversation.item.create"
        and item.get("item", {}).get("type") == "function_call_output"
        for item in fake.sent
    )
    assert not any(
        item.get("type") == "response.create"
        and (item.get("response") or {}).get("tool_choice") == "required"
        for item in fake.sent
    )
    bridge.close()


def test_one_artifact_vs_open_work_is_general(cognitive_isolation) -> None:
    from app.cognitive.artifact import begin_owner_turn, classify_owner_work
    from app.cognitive.context import compile_context
    from app.cognitive.session_store import current

    one = "create a grocery list according to yourself and save it inside my desktop"
    assert classify_owner_work(one) == "one_artifact"
    assert classify_owner_work("make a packing list on my desktop") == "one_artifact"
    assert classify_owner_work("leave a note on my desktop that says call the bank") == "one_artifact"
    assert classify_owner_work("Organize PDFs from this month into the Evie project.") == "open"
    assert classify_owner_work("create a grocery list and a packing list on my desktop") == "open"
    begin_owner_turn(current(), one)
    text = compile_context(
        transcript=one,
        modality="voice",
        device_id="mac",
        cognition=current(),
    )
    assert "one-artifact" in text


@pytest.mark.asyncio
async def test_rephrased_file_effects_do_not_spawn_sibling_writes(
    cognitive_isolation, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive.artifact import begin_owner_turn, bound_artifact
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import current

    calls: list[dict[str, Any]] = []

    async def _mac(name, arguments, *, live_session_id=None):
        del name, live_session_id
        calls.append(dict(arguments or {}))
        return {
            "ok": True,
            "verified": True,
            "executed": True,
            "action": "write",
            "path": "/tmp/evie-artifact-list.txt",
            "receipt": "named_list",
            "label": "grocery",
        }

    monkeypatch.setattr("app.cognitive.edge.execute_on_mac", _mac)
    begin_owner_turn(
        current(),
        "create a grocery list according to yourself and save it inside my desktop",
    )
    first = await execute_semantic(
        db_session,
        "files.act",
        {"effect": "create a grocery list on the desktop"},
        cognition=current(),
        actor="master",
        live_session_id="live-artifact",
        steering_seen=int(current().steering_version),
    )
    second = await execute_semantic(
        db_session,
        "files.act",
        {"effect": "create a text list"},
        cognition=current(),
        actor="master",
        live_session_id="live-artifact",
        steering_seen=int(current().steering_version),
    )
    third = await execute_semantic(
        db_session,
        "files.act",
        {"effect": "verify grocery list"},
        cognition=current(),
        actor="master",
        live_session_id="live-artifact",
        steering_seen=int(current().steering_version),
    )
    fourth = await execute_semantic(
        db_session,
        "files.act",
        {"effect": "create a desktop grocery list"},
        cognition=current(),
        actor="master",
        live_session_id="live-artifact",
        steering_seen=int(current().steering_version),
    )
    assert first.get("ok") is True
    assert first.get("path") == "/tmp/evie-artifact-list.txt"
    assert len(calls) == 1
    assert second.get("artifact_complete") is True
    assert third.get("artifact_complete") is True
    assert fourth.get("artifact_complete") is True
    assert bound_artifact(current()).get("path") == "/tmp/evie-artifact-list.txt"
    assert "ARTIFACT_COMPLETE" in str(second.get("instruction") or "")

