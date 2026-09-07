"""Desk payload meaning: items, not the list kind."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.ev.desk_meaning import (
    extract_inventory,
    interpret_owner_act,
    is_kind_echo,
    leftover_needs_model,
    list_create_parts,
    looks_like_desk_job,
    note_create_parts,
    reject_terms,
    spark_desk_candidate,
)
from app.ev.desk_names import reset_desk_names
from app.ev.laptop_files import parse_file_goal


@pytest.fixture(autouse=True)
def _clear_desk():
    reset_desk_names()
    yield
    reset_desk_names()


@pytest.fixture
def files_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(tmp_path))
    return tmp_path


def test_inventory_uses_list_shape_not_a_says_phrase() -> None:
    phrases = (
        "make a packing list on my Desktop that includes names like passport, charger, tape, and soap",
        'make a packing list on my desktop that includes names like "passport", "charger", "tape", and "soap"',
        "create a packing list on the desktop including passport, charger, tape, and soap",
        "start a packing list with passport, charger, tape and soap",
        "make a packing list: passport, charger, tape, soap",
        "Make a packing list that says passport, charger, tape, soap",
        "make me a grocery list with milk, eggs, and bread",
        "build a camping checklist, tent, stove, water",
    )
    for phrase in phrases:
        deny = reject_terms(phrase)
        items = extract_inventory(phrase, reject=deny)
        lowered = [item.lower() for item in items]
        assert len(items) >= 3, (phrase, items)
        assert "packing" not in lowered, (phrase, items)
        assert "desktop" not in lowered, (phrase, items)
        assert "list" not in lowered, (phrase, items)


def test_kind_echo_is_not_owner_content() -> None:
    assert is_kind_echo("Packing\n", "packing") is True
    assert is_kind_echo("packing list", "packing") is True
    assert is_kind_echo("Note for Monday.", "tomorrow") is True
    assert is_kind_echo("flight tomorrow", "flight") is True
    assert is_kind_echo("passport\ncharger", "packing") is False
    assert is_kind_echo("buy milk", "") is False


def test_empty_list_has_no_leftover_model_need() -> None:
    phrase = "make a packing list on my desktop"
    items = extract_inventory(phrase, reject=reject_terms(phrase, "packing"))
    assert items == []
    assert leftover_needs_model(phrase, items, label="packing") is False


def test_unstructured_remainder_asks_the_model() -> None:
    phrase = "make a packing list of the airport kit we always take"
    items = extract_inventory(phrase, reject=reject_terms(phrase, "packing"))
    assert items == []
    assert leftover_needs_model(phrase, items, label="packing") is True


@pytest.mark.asyncio
async def test_packing_list_writes_named_items_not_the_word_packing(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    phrase = (
        "make a packing list on my Desktop that includes names like "
        "passport, charger, tape, and soap"
    )
    goal = parse_file_goal(phrase)
    assert goal is not None
    assert goal["action"] == "write"
    body = (goal.get("content") or "").lower()
    for token in ("passport", "charger", "tape", "soap"):
        assert token in body
    assert body.strip() not in {"packing", "packing list"}
    assert "packing" not in [item.lower() for item in (goal.get("items") or [])]

    ran = await run_file_goal(goal)
    assert ran["ok"] is True
    landed = Path(ran["path"]).read_text(encoding="utf-8").lower()
    for token in ("passport", "charger", "tape", "soap"):
        assert token in landed
    assert landed.strip() != "packing"
    spoken = str(ran.get("spoken") or "").lower()
    assert "passport" in spoken
    assert "packing" in spoken
    assert "evie-note.txt" not in spoken


@pytest.mark.asyncio
async def test_unstructured_list_uses_muse_spark_not_the_kind_name(
    files_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.contracts import ChatResult
    from app.ev.desk_meaning import spark_inventory
    from app.ev.laptop_files import plan_file_content

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    class _Prov:
        async def chat_structured(self, messages, **kwargs):
            del messages, kwargs
            return ChatResult(
                text='{"items":["passport","charger","tape","soap"],"empty":false}',
                usage={},
                model="muse-spark-1.3-contributor",
            )

        async def chat(self, messages, **kwargs):
            raise AssertionError("structured payload must use chat_structured")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    phrase = "make a packing list of the airport kit we always take"
    items = await spark_inventory(phrase, label="packing")
    assert items == ["passport", "charger", "tape", "soap"]

    body, source = await plan_file_content(
        action="write",
        current="",
        instruction=phrase,
        content="Packing\n",
        label="packing",
        receipt="named_list",
    )
    assert source == "spark"
    lowered = body.lower()
    for token in ("passport", "charger", "tape", "soap"):
        assert token in lowered
    assert lowered.strip() != "packing"


def test_named_list_does_not_require_make_or_create() -> None:
    phrases = (
        "I need a packing list on my Desktop that includes names like passport, charger, tape, and soap",
        "packing list: passport, charger, tape, soap",
        "can you get a grocery list going with milk, eggs, and bread",
        "how about a packing list with passport, charger, and tape",
        "I could use a grocery list with milk, eggs, and bread",
    )
    for phrase in phrases:
        parts = list_create_parts(phrase)
        assert parts is not None, phrase
        lowered = [item.lower() for item in parts["items"]]
        assert len(parts["items"]) >= 3, (phrase, parts)
        assert parts["label"] in {"packing", "grocery"}
        assert "list" not in lowered
        assert "desktop" not in lowered
        goal = parse_file_goal(phrase)
        assert goal is not None and goal["action"] == "write", phrase
        body = (goal.get("content") or "").lower()
        assert "list" not in body.split()
        assert body.strip() not in {"packing", "grocery", "packing list"}


def test_note_create_does_not_require_drop_or_leave() -> None:
    phrase = "note on the desktop: buy milk"
    parts = note_create_parts(phrase)
    assert parts is not None
    assert "milk" in (parts.get("body") or "").lower()
    goal = parse_file_goal(phrase)
    assert goal is not None and goal["action"] == "write"
    assert "milk" in (goal.get("content") or "").lower()


def test_meaning_does_not_steal_chat_or_sleep_reasons() -> None:
    assert looks_like_desk_job("that's fine") is False
    assert looks_like_desk_job("I'm also tired") is False
    assert looks_like_desk_job("just keep going") is False
    assert looks_like_desk_job("how are you") is False
    assert looks_like_desk_job("make a list of reasons I should sleep") is False
    assert parse_file_goal("make a list of reasons I should sleep") is None
    assert spark_desk_candidate("make a list of reasons I should sleep") is False
    assert spark_desk_candidate("that's fine") is False
    assert spark_desk_candidate("what's on the list") is False
    assert spark_desk_candidate("add tape to the packing list") is False
    assert spark_desk_candidate("write a file called notes on my desktop") is False


def test_append_to_named_list_is_not_a_new_list() -> None:
    assert list_create_parts("add tape to the packing list") is None
    assert list_create_parts("add tape to packing list") is None


@pytest.mark.asyncio
async def test_spark_maps_awkward_remainder_onto_a_write_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.contracts import ChatResult

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    class _Prov:
        async def chat_structured(self, messages, **kwargs):
            del messages, kwargs
            return ChatResult(
                text='{"act":"write_list","label":"packing","items":["passport","charger"]}',
                usage={},
                model="muse-spark-1.3-contributor",
            )

        async def chat(self, messages, **kwargs):
            raise AssertionError("act classify must use chat_structured")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    phrase = "whip the usual airport kit into a packing list on the computer"
    assert spark_desk_candidate(phrase) is False or looks_like_desk_job(phrase)
    interpreted = await interpret_owner_act(phrase)
    if interpreted is None:
        from app.ev.laptop_files import plan_file_content

        goal = parse_file_goal(phrase)
        assert goal is not None and goal["action"] == "write"
        body, source = await plan_file_content(
            action="write",
            current="",
            instruction=phrase,
            content=str(goal.get("content") or ""),
            label=str(goal.get("label") or "packing"),
            receipt="named_list",
        )
        assert source == "spark"
        lowered = [line.strip() for line in body.lower().splitlines() if line.strip()]
        assert "passport" in body.lower() and "charger" in body.lower()
        assert "packing" not in lowered
        return
    assert interpreted.get("channel") == "file"
    goal = interpreted["goal"]
    assert goal["action"] == "write"
    lowered = [item.lower() for item in (goal.get("items") or [])]
    assert "passport" in lowered and "charger" in lowered
    assert "packing" not in lowered


@pytest.mark.asyncio
async def test_spark_leaves_feelings_as_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.contracts import ChatResult

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    class _Prov:
        async def chat_structured(self, messages, **kwargs):
            del messages, kwargs
            return ChatResult(text='{"act":"chat"}', usage={}, model="muse-spark-1.3-contributor")

        async def chat(self, messages, **kwargs):
            raise AssertionError("act classify must use chat_structured")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    assert await interpret_owner_act("I'm tired of this packing list on the computer") is None


def test_live_mutate_is_completion_or_exit_not_a_verb_list() -> None:
    from app.ev.desk_meaning import live_list_mutate

    held = ["passport", "charger", "tape", "soap"]
    act, tokens = live_list_mutate("I packed soap and charger", held)
    assert act == "checkoff"
    assert {item.lower() for item in tokens} >= {"soap", "charger"}
    act, tokens = live_list_mutate(
        "remove soap and charger because I packed them", held
    )
    assert act == "drop"
    assert {item.lower() for item in tokens} >= {"soap", "charger"}
    act, tokens = live_list_mutate("I don't need the tape", held)
    assert act == "drop"
    assert any(item.lower() == "tape" for item in tokens)
    assert live_list_mutate("I packed my bags", held) == (None, [])
    assert live_list_mutate("I need soap and charger", held) == (None, [])
    assert live_list_mutate("I love my charger", held) == (None, [])
    assert live_list_mutate("I'm also tired", held) == (None, [])
    assert live_list_mutate("that's fine", held) == (None, [])
    assert live_list_mutate("I got home", held) == (None, [])


def test_full_airport_checklist_is_a_desk_job_without_named_items() -> None:
    from app.ev.desk_meaning import wants_generated_contents

    phrases = (
        "create a full airport check-list file, adding necessary item names needed for travel and airport",
        "I want you to create a full airport checklist, adding necessary items for travel",
    )
    for phrase in phrases:
        parts = list_create_parts(phrase)
        assert parts is not None, phrase
        assert not parts.get("items"), phrase
        assert wants_generated_contents(phrase, [], label=str(parts.get("label") or ""))
        goal = parse_file_goal(phrase)
        assert goal is not None and goal["action"] == "write", phrase
        assert looks_like_desk_job(phrase)
    assert looks_like_desk_job("make a list of reasons I should sleep") is False
    assert spark_desk_candidate("make a list of reasons I should sleep") is False


@pytest.mark.asyncio
async def test_generated_airport_list_writes_travel_items_not_the_title(
    files_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.contracts import ChatResult
    from app.ev.laptop_files import plan_file_content, run_file_goal

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    class _Prov:
        async def chat_structured(self, messages, **kwargs):
            del messages, kwargs
            return ChatResult(
                text='{"items":["passport","boarding pass","charger","toothpaste"],"empty":false}',
                usage={},
                model="muse-spark-1.3-contributor",
            )

        async def chat(self, messages, **kwargs):
            raise AssertionError("structured payload must use chat_structured")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    phrase = (
        "create a full airport check-list file, adding necessary item names "
        "needed for travel and airport"
    )
    goal = parse_file_goal(phrase)
    assert goal is not None
    body, source = await plan_file_content(
        action="write",
        current="",
        instruction=phrase,
        content=str(goal.get("content") or ""),
        label=str(goal.get("label") or ""),
        receipt="named_list",
    )
    assert source == "spark"
    lowered = body.lower()
    for token in ("passport", "charger"):
        assert token in lowered
    assert lowered.strip() not in {"airport", "packing", "checklist"}
    ran = await run_file_goal({**goal, "content": body})
    assert ran["ok"] is True
    landed = Path(ran["path"]).read_text(encoding="utf-8").lower()
    assert "passport" in landed
    assert landed.strip() != "airport"


def test_situation_plus_you_think_is_not_the_file_body() -> None:
    from app.ev.desk_meaning import occasion_label, wants_generated_contents

    phrases = (
        "I have a flight tomorrow, I want you to create a list of items you think is necessary "
        "and make a list and save that text document inside my desktop",
        "I have a flight tomorrow. Make a list of what you think I need on my desktop.",
        "I have an interview tomorrow, put a list of what you think is necessary on my desktop",
    )
    for phrase in phrases:
        assert occasion_label(phrase) in {"flight", "interview"}, phrase
        parts = list_create_parts(phrase)
        assert parts is not None, phrase
        assert not parts.get("items"), (phrase, parts)
        assert parts["label"] in {"flight", "interview", "list"}
        assert wants_generated_contents(phrase, list(parts.get("items") or []), label=parts["label"])
        goal = parse_file_goal(phrase)
        assert goal is not None and goal["action"] == "write", phrase
        body = (goal.get("content") or "").lower()
        assert "flight tomorrow" not in body
        assert "interview tomorrow" not in body
        assert looks_like_desk_job("I have a flight tomorrow") is False


@pytest.mark.asyncio
async def test_flight_tomorrow_list_uses_spark_not_the_occasion(
    files_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.contracts import ChatResult
    from app.ev.laptop_files import plan_file_content, run_file_goal

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    class _Prov:
        async def chat_structured(self, messages, **kwargs):
            del messages, kwargs
            return ChatResult(
                text='{"items":["passport","boarding pass","charger","toothbrush"],"empty":false}',
                usage={},
                model="muse-spark-1.3-contributor",
            )

        async def chat(self, messages, **kwargs):
            raise AssertionError("structured payload must use chat_structured")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    phrase = (
        "I have a flight tomorrow, I want you to create a list of items you think is necessary "
        "and make a list and save that text document inside my desktop"
    )
    goal = parse_file_goal(phrase)
    assert goal is not None
    body, source = await plan_file_content(
        action="write",
        current="",
        instruction=phrase,
        content=str(goal.get("content") or ""),
        label=str(goal.get("label") or ""),
        receipt="named_list",
    )
    assert source == "spark"
    lowered = body.lower()
    assert "passport" in lowered
    assert "flight tomorrow" not in lowered
    assert lowered.strip() not in {"flight", "flight tomorrow", "list"}
    ran = await run_file_goal({**goal, "content": body})
    assert ran["ok"] is True
    landed = Path(ran["path"]).read_text(encoding="utf-8").lower()
    assert "passport" in landed
    assert landed.strip() != "flight tomorrow"


@pytest.mark.asyncio
async def test_generate_without_spark_does_not_write_the_occasion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev.desk_meaning import resolve_write_body

    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    phrase = (
        "I have a flight tomorrow, I want you to create a list of items you think is necessary "
        "and make a list and save that text document inside my desktop"
    )
    body, source, items = await resolve_write_body(
        phrase,
        proposed="flight tomorrow",
        label="flight",
        receipt="named_list",
    )
    assert source == "empty"
    assert not body.strip()
    assert not items
