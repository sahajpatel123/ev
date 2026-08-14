"""WAVE LIFE agency loop: life tools, autonomy, golden path, capability theater."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import settings
from app.contracts import ChatMessage, ChatResult, RequestEnvelope, ToolCall
from app.ev import tool_select
from app.ev import tools as ev_tools
from app.ev.actions import life_agency_prompt, list_action_specs
from app.gateway.service import ModelGateway, tool_specs_from_dicts
from app.integrations import service as integrations
from app.models import Integration
from app.services.tool_loop import run_tool_loop

LIFE_TOOL_NAMES = {
    "send_message",
    "list_messages",
    "resolve_contact",
    "place_call",
    "list_mail",
    "open_url",
    "set_reminder",
}


def _life_spec(name: str) -> dict:
    return next(spec for spec in ev_tools.list_tools() if spec["name"] == name)


def test_life_tools_exist_with_validated_schemas() -> None:
    names = {spec["name"] for spec in ev_tools.list_tools()}
    assert names >= LIFE_TOOL_NAMES
    for name in LIFE_TOOL_NAMES:
        spec = _life_spec(name)
        assert spec["parameters"]["type"] == "object"
        assert spec["permission"]
        # every declared schema round-trips through the gateway converter
        converted = tool_specs_from_dicts([spec])
        assert converted[0].name == name


def test_autonomy_mode_resolves_life_tool_approval(monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_autonomy", "full")
    assert _life_spec("send_message")["sensitive"] is False
    assert _life_spec("place_call")["sensitive"] is False
    assert _life_spec("resolve_contact")["sensitive"] is False

    monkeypatch.setattr(settings, "owner_autonomy", "confirm_all")
    assert _life_spec("send_message")["sensitive"] is True
    assert _life_spec("place_call")["sensitive"] is True
    assert _life_spec("resolve_contact")["sensitive"] is False  # read-only

    monkeypatch.setattr(settings, "owner_autonomy", "confirm_unknown")
    assert _life_spec("send_message")["sensitive"] is False


def test_action_specs_respect_autonomy(monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_autonomy", "full")
    by_name = {spec["name"]: spec for spec in list_action_specs()}
    assert by_name["send_message"]["requires_approval"] is False
    monkeypatch.setattr(settings, "owner_autonomy", "confirm_all")
    by_name = {spec["name"]: spec for spec in list_action_specs()}
    assert by_name["send_message"]["requires_approval"] is True


async def _add_bridge(db_session, *, slug: str, adapter: str, scopes: list[str]) -> Integration:
    row = Integration(
        slug=slug,
        adapter=adapter,
        name=slug,
        scopes=scopes,
        status="active",
        config={"provider": "http"},
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_golden_path_text_mom_im_late(db_session, monkeypatch) -> None:
    """voice transcript → intent → tool calls → adapter result → spoken reply."""

    monkeypatch.setattr(settings, "owner_autonomy", "full")
    await _add_bridge(db_session, slug="contacts", adapter="contacts", scopes=["contacts:read"])
    await _add_bridge(db_session, slug="messaging", adapter="messaging", scopes=["messaging:act"])

    calls: list[tuple[str, str, dict]] = []

    async def fake_execute_action(session, integration_id, action, args, *, actor):
        calls.append((str(integration_id), action, dict(args)))
        if action == "contacts.resolve":
            return SimpleNamespace(
                result={
                    "ok": True,
                    "contact": {"name": "Mom", "phone": "+15551234567"},
                }
            )
        if action == "messaging.send":
            return SimpleNamespace(
                result={
                    "ok": True,
                    "delivery": {
                        "confirmed": True,
                        "evidence": {
                            "recipient": "Mom",
                            "channel": "Messages",
                            "sent_at": "2026-08-13T14:32:00Z",
                        },
                    },
                }
            )
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(integrations, "execute_action", fake_execute_action)

    # voice utterance -> intent
    selection = tool_select.select_tool("text Mom I'm late")
    assert selection.selected == "send_message"
    assert "resolve_contact" in selection.alternatives

    # tool call: resolve the contact
    resolved = await ev_tools.dispatch(
        db_session,
        "resolve_contact",
        {"name": "Mom"},
        actor="owner",
    )
    assert resolved.ok is True
    assert resolved.result is not None
    assert resolved.result["contact"]["phone"] == "+15551234567"

    # tool call: send through the adapter
    sent = await ev_tools.dispatch(
        db_session,
        "send_message",
        {"to": "Mom", "text": "I'm late"},
        actor="owner",
    )
    assert sent.ok is True
    assert sent.result is not None
    assert sent.result["delivery"]["confirmed"] is True
    assert sent.result["delivery"]["evidence"]["recipient"] == "Mom"

    # spoken reply with evidence
    reply = ev_tools.life_success_reply(sent.result)
    assert reply == "Sent to Mom via Messages at 2026-08-13T14:32:00Z."

    actions = {action for _id, action, _args in calls}
    assert actions == {"contacts.resolve", "messaging.send"}


class LifeLoopProvider:
    """Mock provider: one life tool call, then a spoken confirmation."""

    name = "life-loop"
    supports_media = False

    def __init__(self) -> None:
        self.rounds = 0

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        return ChatResult(text="done", usage={}, model=model or self.name)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7) -> ChatResult:
        self.rounds += 1
        if self.rounds == 1:
            return ChatResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="life-1",
                        name="send_message",
                        arguments={"to": "Mom", "text": "I'm late"},
                    )
                ],
                usage={},
                model=model or self.name,
            )
        return ChatResult(
            text="Sent to Mom via Messages at 2026-08-13T14:32:00Z.",
            usage={},
            model=model or self.name,
        )

    async def list_models(self) -> list[str]:
        return [self.name]


async def test_tool_loop_proves_life_path_with_mocked_adapter(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_autonomy", "full")
    await _add_bridge(db_session, slug="messaging", adapter="messaging", scopes=["messaging:act"])

    async def fake_execute_action(session, integration_id, action, args, *, actor):
        return SimpleNamespace(
            result={
                "ok": True,
                "delivery": {
                    "confirmed": True,
                    "evidence": {
                        "recipient": "Mom",
                        "channel": "Messages",
                        "sent_at": "2026-08-13T14:32:00Z",
                    },
                },
            }
        )

    monkeypatch.setattr(integrations, "execute_action", fake_execute_action)
    provider = LifeLoopProvider()
    gateway = ModelGateway(provider)
    call = await run_tool_loop(
        db_session,
        gateway,
        [ChatMessage(role="user", content="text Mom I'm late")],
        envelope=RequestEnvelope(request_id="life-loop-1", strategy={}),
        tool_specs=tool_specs_from_dicts([_life_spec("send_message")]),
        actor="owner",
    )
    assert call.status == "ok"
    assert "Sent to Mom via Messages" in call.result.text
    assert provider.rounds == 2


async def test_missing_bridge_is_honest_capability_theater(db_session) -> None:
    """No bridge installed -> degraded=true + exact next_step, never fake success."""

    sent = await ev_tools.dispatch(
        db_session,
        "send_message",
        {"to": "Mom", "text": "I'm late"},
        actor="owner",
    )
    assert sent.ok is True
    assert sent.result is not None
    assert sent.result["degraded"] is True
    assert "messaging" in sent.result["next_step"]
    assert "grant scope" in sent.result["next_step"]

    opened = await ev_tools.dispatch(
        db_session,
        "open_url",
        {"url": "https://example.com"},
        actor="owner",
    )
    assert opened.result is not None
    assert opened.result["degraded"] is True
    assert "open-url bridge" in opened.result["next_step"]

    reminded = await ev_tools.dispatch(
        db_session,
        "set_reminder",
        {"text": "stand up"},
        actor="owner",
    )
    assert reminded.result is not None
    assert reminded.result["degraded"] is True
    assert "reminders bridge" in reminded.result["next_step"]

    search = await ev_tools.dispatch(
        db_session,
        "search_web",
        {"query": "anything"},
        actor="owner",
    )
    assert search.result is not None
    assert search.result["degraded"] is True
    assert "EV_SEARCH_PROVIDER" in search.result["next_step"]


class CapturingProvider:
    name = "capture-life"
    supports_media = False

    def __init__(self) -> None:
        self.seen_messages: list[ChatMessage] = []

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        self.seen_messages = list(messages)
        return ChatResult(text="ok", usage={}, model=model or self.name)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7) -> ChatResult:
        self.seen_messages = list(messages)
        return ChatResult(text="ok", usage={}, model=model or self.name)

    async def list_models(self) -> list[str]:
        return [self.name]


async def test_life_agency_prompt_injected_when_life_tools_offered() -> None:
    provider = CapturingProvider()
    gateway = ModelGateway(provider)
    await gateway.chat(
        [ChatMessage(role="system", content="identity"), ChatMessage(role="user", content="text Mom")],
        tools=tool_specs_from_dicts([_life_spec("send_message")]),
    )
    system_text = " ".join(m.content for m in provider.seen_messages if m.role == "system")
    assert "LIFE AGENCY" in system_text
    assert "owner's agent" in system_text
    assert life_agency_prompt() in system_text


async def test_life_agency_prompt_not_injected_without_life_tools() -> None:
    provider = CapturingProvider()
    gateway = ModelGateway(provider)
    await gateway.chat(
        [ChatMessage(role="system", content="identity"), ChatMessage(role="user", content="hi")],
        tools=tool_specs_from_dicts([_life_spec("resolve_contact")]),
    )
    system_text = " ".join(m.content for m in provider.seen_messages if m.role == "system")
    assert "LIFE AGENCY" in system_text  # contacts:read is also a life scope

    provider2 = CapturingProvider()
    gateway2 = ModelGateway(provider2)
    await gateway2.chat(
        [ChatMessage(role="system", content="identity"), ChatMessage(role="user", content="2+2")],
        tools=tool_specs_from_dicts([_life_spec("calculate")]),
    )
    system_text2 = " ".join(m.content for m in provider2.seen_messages if m.role == "system")
    assert "LIFE AGENCY" not in system_text2
