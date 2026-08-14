"""Intelligence briefing: pre-dispatch existing layers before the LLM."""

from __future__ import annotations

from app.ev.briefing import (
    extract_expression,
    gather_intelligence_briefing,
    infer_args,
    tools_for_turn,
)
from app.ev.personality import identity_block
from app.ev.tool_select import select_tool
from app.ev.tools import list_tools


def test_identity_names_evie_and_capabilities() -> None:
    block = identity_block("EV", "the owner's personal AI")
    assert "EVIE" in block
    assert "DeepSeek" in block
    assert "present" in block
    assert "weather" in block.lower()


def test_select_tool_covers_assistant_range() -> None:
    assert select_tool("what's the weather in Surat?").selected == "get_weather"
    assert select_tool("what is the capital of France").selected == "search_web"
    assert select_tool("what's on my calendar today").selected == "get_upcoming_alerts"
    assert select_tool("when should I leave").selected == "get_upcoming_alerts"
    assert select_tool("how was my sleep this week?").selected == "get_health_trends"
    assert select_tool("what's 14% of 3500").selected == "calculate"
    assert select_tool("text Mom I'm late").selected == "send_message"
    assert select_tool("keep an eye on the deadline").selected == "present"


def test_infer_args_for_reads() -> None:
    assert infer_args("present", "show me that on screen") is None
    assert infer_args("send_message", "text Mom I'm late") is None
    calc = infer_args("calculate", "what's 25 * 4?")
    assert calc == {"expression": "25 * 4"}
    percent = infer_args("calculate", "what's 14% of 3500")
    assert percent is not None and "14" in percent["expression"]
    person = infer_args("get_person", "where is my friend Maya?")
    assert person == {"name": "Maya"}
    contact = infer_args("resolve_contact", "text Mom I'm late")
    assert contact is not None and contact["name"] == "Mom"
    weather = infer_args("get_weather", "weather in London")
    assert weather is not None and weather.get("place") == "London"


def test_extract_expression() -> None:
    assert extract_expression("what's 25 * 4?") == "25 * 4"
    assert extract_expression("what's 14% of 3,500") == "(14/100)*3500"


def test_tools_for_turn_is_focused() -> None:
    all_names = {spec["name"] for spec in list_tools()}
    weather = {spec["name"] for spec in tools_for_turn("what's the weather")}
    assert "get_weather" in weather
    assert "search_web" in weather
    assert "present" in weather
    assert len(weather) < len(all_names)
    assert "place_call" not in weather

    life = {spec["name"] for spec in tools_for_turn("text Mom I'm late")}
    assert "send_message" in life
    assert "resolve_contact" in life
    assert "place_call" in life


async def test_briefing_includes_clock_and_math(db_session) -> None:
    text = await gather_intelligence_briefing(
        db_session,
        "what's 25 * 4?",
        actor="master",
        allow_sensitive=False,
        source="voice",
    )
    assert text is not None
    assert "Local time:" in text
    assert "100" in text
    assert "calculate" in text
    assert "spoken" in text.lower()
    assert "Tool send_message:" not in text


async def test_briefing_does_not_auto_send(db_session) -> None:
    text = await gather_intelligence_briefing(
        db_session,
        "text Mom I'm late",
        actor="master",
        allow_sensitive=False,
        source="chat",
    )
    assert text is not None
    assert "write action" in text.lower() or "send_message" in text
    assert '"name": "send_message"' not in text
    assert "Tool send_message:" not in text
