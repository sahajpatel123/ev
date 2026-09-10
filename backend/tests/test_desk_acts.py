"""Desk acts: undo, check-off, which-note, list remind/text, named and dated notes."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.ev.desk_acts import parse_desk_act, parse_desk_file_goal
from app.ev.desk_names import reset_desk_names
from app.ev.laptop_files import looks_like_file_task, parse_file_goal
from app.ev.resolve import owner_now
from app.ev.tool_select import resolve_live_action


@pytest.fixture(autouse=True)
def _clear_desk_names():
    reset_desk_names()
    yield
    reset_desk_names()


@pytest.fixture
def files_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_named_list_create_is_not_a_generic_note(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    phrase = "Make a packing list that says passport, charger"
    goal = parse_file_goal(phrase)
    assert goal is not None
    assert goal["action"] == "write"
    assert "packing" in str(goal.get("path") or goal.get("query") or "").lower()
    assert "passport" in (goal.get("content") or "")
    assert "charger" in (goal.get("content") or "")
    ran = await run_file_goal(goal)
    assert ran["ok"] is True
    path = Path(ran["path"])
    assert "packing" in path.name.lower()
    assert path.name != "evie-note.txt"
    body = path.read_text(encoding="utf-8")
    assert "passport" in body and "charger" in body
    spoken = str(ran.get("spoken") or "")
    assert "packing" in spoken.lower()
    assert "passport" in spoken.lower()

    again = parse_file_goal("start a packing list with tape and soap")
    assert again is not None and again["action"] == "write"
    todo = parse_file_goal("create a todo list: charger, soap")
    assert todo is not None and todo["action"] == "write"
    assert "todo" in str(todo.get("query") or todo.get("path") or "").lower()
    assert "charger" in (todo.get("content") or "")
    check = parse_file_goal("make me a checklist with oats and honey")
    assert check is not None and "checklist" in str(check.get("query") or "").lower()


@pytest.mark.asyncio
async def test_dated_note_for_tomorrow(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    stamp = (owner_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    weekday = (owner_now() + timedelta(days=1)).strftime("%A")
    phrase = "Leave a note for tomorrow: call the bank"
    goal = parse_file_goal(phrase)
    assert goal is not None
    assert goal["action"] == "write"
    assert stamp in str(goal.get("path") or "") + str(goal.get("query") or "")
    ran = await run_file_goal(goal)
    assert ran["ok"] is True
    path = Path(ran["path"])
    assert stamp in path.name
    assert "call the bank" in path.read_text(encoding="utf-8").lower()
    spoken = str(ran.get("spoken") or "").lower()
    assert weekday.lower() in spoken or "tomorrow" in spoken
    assert "call the bank" in spoken

    alt = parse_file_goal("drop a note for tomorrow that says call the bank")
    assert alt is not None and alt["action"] == "write"
    monday = parse_file_goal("leave a note for Monday: pick up tape")
    assert monday is not None and monday["action"] == "write"
    assert "tape" in (monday.get("content") or "").lower()
    assert str(monday.get("query") or "").startswith("note-")


@pytest.mark.asyncio
async def test_undo_restores_last_list_change(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    written = await run_file_goal(
        parse_file_goal("drop a note on the desktop that says pick up dry cleaning")
    )
    assert written["ok"] is True
    path = Path(written["path"])
    added = parse_file_goal("add 2 more things, oats and honey")
    assert added is not None and added["action"] == "append"
    ran = await run_file_goal(added)
    assert ran["ok"] is True
    spoken = str(ran.get("spoken") or "").lower()
    assert "oats" in spoken and "honey" in spoken
    assert "on the list" in spoken
    body = path.read_text(encoding="utf-8")
    assert "oats" in body and "honey" in body

    for phrase in ("undo that", "put it back", "revert that"):
        undo = parse_file_goal(phrase)
        assert undo is not None and undo["action"] == "undo", phrase

    undone = await run_file_goal(parse_file_goal("put it back"))
    assert undone["ok"] is True
    assert "oats" not in path.read_text(encoding="utf-8")
    assert "pick up dry cleaning" in path.read_text(encoding="utf-8")
    assert "put it back" in str(undone.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_checkoff_marks_done_and_does_not_delete(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    written = await run_file_goal(
        parse_file_goal("drop a note on the desktop that says pick up dry cleaning")
    )
    assert written["ok"] is True
    path = Path(written["path"])
    await run_file_goal(parse_file_goal("put oats and honey on there too"))
    assert looks_like_file_task("I got the oats")
    got = parse_file_goal("I got the oats")
    assert got is not None and got["action"] == "checkoff"
    ran = await run_file_goal(got)
    assert ran["ok"] is True
    body = path.read_text(encoding="utf-8")
    assert "oats" in body.lower()
    assert "[x]" in body.lower()
    assert "honey" in body
    assert "checked off" in str(ran.get("spoken") or "").lower()
    assert "oats" in str(ran.get("spoken") or "").lower()

    crossed = parse_file_goal("cross off dry cleaning")
    assert crossed is not None and crossed["action"] == "checkoff"
    await run_file_goal(crossed)
    later = path.read_text(encoding="utf-8").lower()
    assert "dry cleaning" in later
    assert later.count("[x]") >= 2


@pytest.mark.asyncio
async def test_which_note_asks_then_binds_choice(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    packing = await run_file_goal(
        parse_file_goal("Make a packing list that says passport, charger")
    )
    assert packing["ok"] is True
    grocery = await run_file_goal(
        parse_file_goal("drop a note on the desktop that says milk")
    )
    assert grocery["ok"] is True
    asked = parse_file_goal("add rice")
    assert asked is not None and asked["action"] == "ask_which"
    spoken = str(asked.get("spoken") or "").lower()
    assert "which note" in spoken
    ran = await run_file_goal(asked)
    assert ran["ok"] is True
    assert "which note" in str(ran.get("spoken") or "").lower()
    assert "rice" not in Path(packing["path"]).read_text(encoding="utf-8").lower()
    assert "rice" not in Path(grocery["path"]).read_text(encoding="utf-8").lower()

    for phrase in ("the packing one", "packing", "the packing list"):
        chosen = parse_file_goal(phrase)
        assert chosen is not None and chosen["action"] == "append", phrase
        assert Path(chosen["path"]).resolve() == Path(packing["path"]).resolve()

    applied = await run_file_goal(parse_file_goal("the packing one"))
    assert applied["ok"] is True
    assert "rice" in Path(packing["path"]).read_text(encoding="utf-8").lower()
    assert "rice" not in Path(grocery["path"]).read_text(encoding="utf-8").lower()

    deictic = parse_file_goal("add tape to it")
    assert deictic is not None and deictic["action"] == "append"


@pytest.mark.asyncio
async def test_remind_and_text_use_the_live_list_not_file_op(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    written = await run_file_goal(
        parse_file_goal("Make a packing list that says passport, charger")
    )
    assert written["ok"] is True
    last = str(written["path"])
    phrase = "remind me about this at 7"
    assert looks_like_file_task(phrase, last_path=last) is False
    act = parse_desk_act(phrase, last_path=last)
    assert act is not None and act.get("channel") == "tool"
    assert act["name"] == "set_reminder"
    assert "passport" in str(act["args"].get("text") or "").lower()
    resolved = resolve_live_action(phrase)
    assert resolved is not None and resolved[0] == "set_reminder"

    alt = parse_desk_act("set a reminder for this list tomorrow", last_path=last)
    assert alt is not None and alt.get("name") == "set_reminder"

    texted = parse_desk_act("text this list to Mom", last_path=last)
    assert texted is not None and texted.get("channel") == "tool"
    assert texted["name"] == "send_message"
    assert texted["args"]["to"].lower() == "mom"
    assert "passport" in str(texted["args"].get("text") or "").lower()
    resolved_text = resolve_live_action("send this list to Mom")
    assert resolved_text is not None and resolved_text[0] == "send_message"

    generic = resolve_live_action("remind me to call mom")
    assert generic is not None and generic[0] == "set_reminder"
    assert "call mom" in str(generic[1].get("text") or "").lower()
    late = resolve_live_action("text Mom I'm late")
    assert late is not None and late[0] == "send_message"
    assert "late" in str(late[1].get("text") or "").lower()

    import json

    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps({"ok": True, "spoken": "Reminder set."})

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            return True

    live = LiveSession(session_id="owner-desk-list-tool", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    try:
        handled = await live._maybe_local_intent("remind me about this at 7", from_grok=True)
        assert handled is True
        assert seen and seen[0][0] == "set_reminder"
        assert seen[0][2] == "owner-desk"
        assert "passport" in str(seen[0][1].get("text") or "").lower()
    finally:
        live.close()


def test_desk_acts_do_not_steal_chat(files_root: Path) -> None:
    assert parse_file_goal("Make a packing list that says tape, passport") is not None
    assert parse_desk_file_goal("undo my calendar") is None
    assert parse_desk_file_goal("I got it") is None
    assert parse_desk_file_goal("I got home") is None
    assert parse_desk_file_goal("check with me at my desk") is None
    assert parse_desk_file_goal("that's fine") is None
    assert looks_like_file_task("make a list of reasons I should sleep") is False


@pytest.mark.asyncio
async def test_honest_receipt_names_the_note_body_not_the_filename(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    ran = await run_file_goal(
        parse_file_goal("drop a note on the desktop that says buy milk")
    )
    assert ran["ok"] is True
    spoken = str(ran.get("spoken") or "").lower()
    assert "milk" in spoken
    assert "evie-note.txt" not in spoken
    assert "wrote" not in spoken


@pytest.mark.asyncio
async def test_live_job_holds_the_thread_without_reteaching(files_root: Path) -> None:
    from app.ev.desk_presence import live_work_block, parse_presence_spoken
    from app.ev.laptop_files import extract_append_items, run_file_goal
    from app.ev.personality import identity_block
    from app.voice.live.session import LiveSession

    written = await run_file_goal(
        parse_file_goal("Make a packing list that says passport, charger")
    )
    assert written["ok"] is True
    assert extract_append_items("tape and soap") == ["tape", "soap"]
    assert extract_append_items("the charger too") == ["charger"]
    assert extract_append_items("that's fine") == []
    assert extract_append_items("I'm also tired") == []
    assert extract_append_items("List the files") == []
    assert extract_append_items("read it") == []
    assert extract_append_items("delete everything and just keep bravo") == []
    assert extract_append_items("just keep going") == []
    assert extract_append_items("Open Notes") == []
    assert looks_like_file_task("Open Notes") is False

    added = parse_file_goal("tape and soap")
    assert added is not None and added["action"] == "append"
    ran = await run_file_goal(added)
    assert ran["ok"] is True
    body = Path(written["path"]).read_text(encoding="utf-8").lower()
    assert "tape" in body and "soap" in body
    assert "tape" in str(ran.get("spoken") or "").lower()

    inventory = parse_presence_spoken("what's on it")
    assert inventory is not None
    assert "passport" in inventory.lower()
    job = parse_presence_spoken("what are we working on")
    assert job is not None
    assert "packing" in job.lower()
    block = live_work_block()
    assert "LIVE JOB" in block
    assert "packing" in block.lower()
    ident = identity_block("EVIE", "the owner's operator", compact=True)
    assert "do not interview" in ident.lower() or "hold the thread" in ident.lower() or "already know the current owner task" in ident.lower()
    from app.voice.live.grok_voice import grok_voice_instructions, openai_realtime_instructions

    grok_text = grok_voice_instructions().lower()
    openai_text = openai_realtime_instructions().lower()
    assert "packing" in grok_text and "passport" in grok_text
    assert "packing" in openai_text and "passport" in openai_text

    import json

    spoken: list[str] = []
    refreshed = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        parsed = parse_file_goal(str(args.get("goal") or ""))
        assert parsed is not None
        result = await run_file_goal(parsed)
        return json.dumps({"ok": result.get("ok"), "spoken": result.get("spoken")})

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def refresh_live_instructions(self) -> bool:
            refreshed["n"] += 1
            return True

    live = LiveSession(session_id="owner-desk-presence", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    try:
        handled = await live._maybe_local_intent("what's on it", from_grok=True)
        assert handled is True
        assert spoken and "passport" in spoken[0].lower()
        live._last_honesty = ""
        live._last_life_action = None
        handled = await live._maybe_local_intent("that's fine", from_grok=True)
        assert handled is False
        live._last_honesty = ""
        live._last_life_action = None
        handled = await live._maybe_local_intent("the charger too", from_grok=True)
        assert handled is True
        assert refreshed["n"] >= 1
        assert any("charger" in line.lower() for line in spoken)
        assert "charger" in Path(written["path"]).read_text(encoding="utf-8").lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_packed_and_remove_mutate_the_live_list_without_undo(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    written = await run_file_goal(
        parse_file_goal("Make a packing list that says passport, charger, tape, soap")
    )
    assert written["ok"] is True
    path = Path(written["path"])
    last = str(path)
    packed = parse_file_goal("I packed soap and charger", last_path=last)
    assert packed is not None and packed["action"] == "checkoff", packed
    ran = await run_file_goal(packed)
    assert ran["ok"] is True
    body = path.read_text(encoding="utf-8").lower()
    assert "soap" in body and "[x]" in body
    assert "passport" in body
    assert "checked off" in str(ran.get("spoken") or "").lower()

    dropped = parse_file_goal(
        "remove tape because I packed it", last_path=last
    )
    assert dropped is not None and dropped["action"] == "drop", dropped
    ran = await run_file_goal(dropped)
    assert ran["ok"] is True
    later = path.read_text(encoding="utf-8").lower()
    assert "tape" not in later
    assert "passport" in later
    assert "took" in str(ran.get("spoken") or "").lower()
    assert parse_file_goal("I packed my bags", last_path=last) is None
    assert parse_file_goal("I need soap", last_path=last) is None


@pytest.mark.asyncio
async def test_rephrased_creates_stay_on_one_file(files_root: Path) -> None:
    from app.ev.laptop_files import resolve_file_computer_goal, run_file_goal

    first = parse_file_goal(
        "Make a packing list that says passport, charger, tape, soap"
    )
    written = await run_file_goal(first)
    assert written["ok"] is True
    path = Path(written["path"])
    assert path.is_file()

    text_list = resolve_file_computer_goal("create a text list", last_path=str(path))
    assert text_list is not None
    assert Path(str(text_list[1].get("path") or "")).resolve() == path.resolve()
    assert text_list[1].get("action") in {"read", "write"}
    if text_list[1].get("action") == "write":
        assert text_list[1].get("unique_name") is False
        assert text_list[1].get("overwrite") is True
    await run_file_goal(text_list[1])

    verify = resolve_file_computer_goal("verify the packing list", last_path=str(path))
    assert verify is not None
    assert verify[1].get("action") == "read"
    assert Path(str(verify[1].get("path") or "")).resolve() == path.resolve()
    await run_file_goal(verify[1])

    desktop = resolve_file_computer_goal("create a desktop packing list", last_path=str(path))
    assert desktop is not None
    assert Path(str(desktop[1].get("path") or "")).resolve() == path.resolve()
    await run_file_goal(desktop[1])

    txts = [item for item in files_root.rglob("*.txt") if item.is_file()]
    assert len(txts) == 1, [str(item) for item in txts]
    assert txts[0].resolve() == path.resolve()

    other = parse_file_goal("Make a grocery list that says milk, eggs, bread")
    assert other is not None
    other_path = Path(str(other.get("path") or ""))
    assert other_path.name != path.name
