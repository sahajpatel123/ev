"""Owner laptop files: parse, policy, read/write/edit, Luna rewrite."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.ev.computer_strategy import resolve_generic_computer_goal
from app.ev.laptop_files import (
    apply_simple_edit,
    looks_like_file_task,
    parse_file_goal,
    path_denied,
    perform_local,
    plan_file_content,
    resolve_file_computer_goal,
)
from app.ev.luna_code import looks_like_code_request
from app.ev.tool_select import resolve_live_action
from app.ev.desk_names import reset_desk_names


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


def test_parse_write_read_edit_on_desktop(files_root: Path) -> None:
    write = parse_file_goal(
        "Write a file called evie-proof.txt on my desktop that says hello from evie"
    )
    assert write is not None
    assert write["action"] == "write"
    assert write["path"].endswith("evie-proof.txt")
    assert write["content"] == "hello from evie"

    read = parse_file_goal("Read evie-proof.txt on my desktop")
    assert read is not None and read["action"] == "read"

    edit = parse_file_goal(
        "Edit evie-proof.txt on my desktop and change hello from evie to hi from evie"
    )
    assert edit is not None and edit["action"] == "edit"
    assert "change hello from evie to hi from evie" in (edit.get("instruction") or "").lower() or (
        "hello from evie" in (edit.get("instruction") or "")
    )
    simple = apply_simple_edit(
        "sidecar file proof",
        parse_file_goal(
            "Edit EV-sidecar-files.txt on my desktop and change sidecar file proof to sidecar edit proof"
        )["instruction"],
    )
    assert simple == "sidecar edit proof"

    listed = parse_file_goal("List the files on my desktop")
    assert listed is not None and listed["action"] == "list"


def test_file_goals_do_not_steal_apps_or_code() -> None:
    assert resolve_file_computer_goal("Open TextEdit and type hello world") is None
    assert resolve_generic_computer_goal("Open TextEdit and type hello world") is not None
    assert looks_like_file_task("write a python script that prints hello world") is False
    assert looks_like_code_request("write a python script that prints hello world")
    assert looks_like_code_request("run hello.py")
    assert looks_like_file_task("run hello.py") is False
    assert resolve_live_action("run hello.py") == ("code", {"goal": "run hello.py"})
    desktop = "Write a file called evie-proof.txt on my desktop that says hello from evie"
    assert looks_like_file_task(desktop)
    assert not looks_like_code_request(desktop)
    assert resolve_live_action(desktop) == ("computer", {"goal": desktop})


def test_secrets_are_denied(files_root: Path) -> None:
    ssh = Path.home() / ".ssh" / "id_rsa"
    assert path_denied(ssh) == "path_denied"
    env_file = files_root / ".env"
    env_file.write_text("SECRET=1", encoding="utf-8")
    assert path_denied(env_file) == "path_denied"


def test_write_read_edit_roundtrip(files_root: Path) -> None:
    written = perform_local(
        {
            "action": "write",
            "path": str(files_root / "evie-proof.txt"),
            "content": "hello from evie",
        }
    )
    assert written["ok"] is True
    assert (files_root / "evie-proof.txt").read_text(encoding="utf-8") == "hello from evie"
    read = perform_local({"action": "read", "path": str(files_root / "evie-proof.txt")})
    assert read["ok"] is True
    assert "hello from evie" in read["content"]
    edited = apply_simple_edit("hello from evie", "change hello from evie to hi from evie")
    assert edited == "hi from evie"
    saved = perform_local(
        {
            "action": "write",
            "path": str(files_root / "evie-proof.txt"),
            "content": edited,
        }
    )
    assert saved["ok"] is True
    assert (files_root / "evie-proof.txt").read_text(encoding="utf-8") == "hi from evie"
    names = perform_local({"action": "list", "path": str(files_root)})
    assert "evie-proof.txt" in names["files"]


@pytest.mark.asyncio
async def test_computer_goal_writes_local_file(db_session, files_root: Path) -> None:
    from app.ev.tools import _run_computer_goal

    result = await _run_computer_goal(
        db_session,
        {
            "goal": "Write a file called evie-proof.txt on my desktop that says hello from evie"
        },
        actor="master",
        live_session_id=None,
        device_id=None,
    )
    assert result["ok"] is True
    assert result.get("verified") is True
    assert (files_root / "evie-proof.txt").read_text(encoding="utf-8") == "hello from evie"
    assert result.get("intelligence") in {None, "literal"}

    edited = await _run_computer_goal(
        db_session,
        {
            "goal": "Edit evie-proof.txt on my desktop and change hello from evie to hi from evie"
        },
        actor="master",
        live_session_id=None,
        device_id=None,
    )
    assert edited["ok"] is True
    assert (files_root / "evie-proof.txt").read_text(encoding="utf-8") == "hi from evie"
    assert edited.get("intelligence") == "replace"


@pytest.mark.asyncio
async def test_luna_edit_when_replace_is_not_literal(files_root: Path, monkeypatch) -> None:
    target = files_root / "note.txt"
    target.write_text("The sky is grey.\n", encoding="utf-8")

    async def fake_rewrite(current: str, instruction: str, *, create: bool):
        del create
        assert "warmer" in instruction.lower() or "sky" in current.lower()
        return "The sky is bright.\n", "luna"

    monkeypatch.setattr("app.ev.laptop_files._intelligent_rewrite", fake_rewrite)
    planned, source = await plan_file_content(
        action="edit",
        current="The sky is grey.\n",
        instruction="Make the sentence warmer",
        content="",
    )
    assert planned == "The sky is bright.\n"
    assert source == "luna"


def test_production_api_refuses_without_flag(files_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "laptop_files", False)
    monkeypatch.setattr(settings, "laptop_files_root", None)
    result = perform_local(
        {"action": "write", "path": str(files_root / "nope.txt"), "content": "x"}
    )
    assert result["ok"] is False
    assert result["error"] == "laptop_files_disabled"


@pytest.mark.asyncio
async def test_live_mac_file_op_works_when_api_flag_is_off(monkeypatch) -> None:
    """Talk with EV.app attached must not die on a sidecar missing EV_LAPTOP_FILES."""

    from app.ev.laptop_files import execute_file_op

    monkeypatch.setattr(settings, "environment", "dev")
    monkeypatch.setattr(settings, "laptop_files", False)
    monkeypatch.setattr(settings, "laptop_files_root", None)
    bare = await execute_file_op(
        {"action": "write", "path": "/tmp/nope.txt", "content": "x"}
    )
    assert bare["ok"] is False
    assert bare["error"] == "laptop_files_disabled"
    assert "not enabled" in (bare.get("spoken") or "").lower()

    class _Live:
        async def request_computer(self, command, arguments=None, *, timeout=12.0, request_id=None):
            assert command == "file_op"
            assert arguments["action"] == "write"
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "action": "write",
                "spoken": "Wrote evie-desk-test.txt.",
            }

    live = await execute_file_op(
        {
            "action": "write",
            "path": "/Users/sahajpatel/Desktop/evie-desk-test.txt",
            "content": "hello from sahaj",
        },
        live=_Live(),
        request_id="owner-file",
    )
    assert live["ok"] is True
    assert live["spoken"] == "Wrote evie-desk-test.txt."


def test_spoken_defaults_and_folder_only_write(files_root: Path) -> None:
    assert looks_like_file_task("make a file on the desktop")
    assert looks_like_file_task("write a file on my desk top")
    assert looks_like_file_task("the files on my desktop")
    assert looks_like_file_task("index.html inside my desktop")
    assert not looks_like_file_task(
        "(system confirmation — speak this to the owner now) Wrote as Sahaj-File-Test.txt."
    )
    assert not looks_like_file_task("On Desktop: as Sahaj-File-Test.txt, index.html")
    make = parse_file_goal("make a file on the desktop")
    assert make is not None
    assert make["action"] == "write"
    assert str(make["path"]).endswith("evie-note.txt")
    save = parse_file_goal("save a text file to my desktop")
    assert save is not None and str(save["path"]).endswith("evie-note.txt")
    notes = parse_file_goal("write a file called notes on my desktop")
    assert notes is not None and str(notes["path"]).endswith("notes.txt")
    jot = parse_file_goal("jot down hello from evie on the desktop")
    assert jot is not None and jot["content"] == "hello from evie"
    named = parse_file_goal("write notes on my desktop")
    assert named is not None and str(named["path"]).endswith("notes.txt")

    owner_write = parse_file_goal(
        "I want you to write a file which is inside my desktop folder named as "
        "Sahaj-File-Test.txt, spelled as S-A-H-A-J hyphen F-I-L-E hyphen T-E-S-T "
        "full stop T-X-T."
    )
    assert owner_write is not None
    assert owner_write["action"] == "write"
    assert str(owner_write["path"]).endswith("Sahaj-File-Test.txt")
    assert not str(owner_write["path"]).endswith("as Sahaj-File-Test.txt")
    assert not owner_write.get("instruction")

    listed = parse_file_goal("Can you list all the files on my desktop?")
    assert listed is not None and listed["action"] == "list"
    opened = parse_file_goal("index.html inside my desktop")
    assert opened is not None and opened["action"] == "open"
    assert str(opened["path"]).endswith("index.html")

    folder_write = perform_local(
        {"action": "write", "path": str(files_root), "content": "folder proof"}
    )
    assert folder_write["ok"] is True
    dest = Path(folder_write["path"])
    assert dest.is_file()
    assert dest.parent == files_root
    assert dest.read_text(encoding="utf-8") == "folder proof"

    second = perform_local(
        {"action": "write", "path": str(files_root), "content": "second note"}
    )
    assert second["ok"] is True
    second_path = Path(second["path"])
    assert second_path != dest
    assert second_path.read_text(encoding="utf-8") == "second note"

    read_newest = perform_local({"action": "read", "path": str(files_root)})
    assert read_newest["ok"] is True
    assert "second note" in str(read_newest.get("content") or "")


@pytest.mark.asyncio
async def test_live_openai_transcript_writes_file(files_root: Path) -> None:
    """Realtime with function calls still executes owner file commands."""

    import json

    from app.ev.laptop_files import run_file_goal
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        parsed = parse_file_goal(str(args.get("goal") or ""))
        assert parsed is not None
        result = await run_file_goal(parsed)
        return json.dumps(
            {
                "ok": result.get("ok"),
                "result": result,
                "spoken": result.get("spoken"),
            }
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True

        async def cancel(self) -> None:
            cancelled["n"] += 1

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-file-talk", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    goal = "Write a file called evie-talk-proof.txt on my desktop that says hello from talk"
    try:
        await live.emit(
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime")
        )
        assert cancelled["n"] == 1
        assert seen == [("computer", {"goal": goal, "session_id": "owner-file-talk"}, "owner-file")]
        assert (files_root / "evie-talk-proof.txt").read_text(encoding="utf-8") == "hello from talk"
        assert spoken
        assert "evie-talk-proof.txt" in spoken[0] or "Wrote" in spoken[0]
        live._last_life_action = None
        await live.emit(
            FinalTranscriptEvent(
                at_ms=2,
                text="(system confirmation — speak this to the owner now) Wrote evie-talk-proof.txt.",
                provider="openai-realtime",
            )
        )
        assert seen == [("computer", {"goal": goal, "session_id": "owner-file-talk"}, "owner-file")]
        live._last_life_action = None
        for extra in (
            "Read evie-talk-proof.txt on my desktop",
            "Edit evie-talk-proof.txt on my desktop and change hello from talk to hi from talk",
            "List the files on my desktop",
            "Open index.html on my desktop",
        ):
            await live.emit(
                FinalTranscriptEvent(at_ms=3, text=extra, provider="openai-realtime")
            )
            live._last_life_action = None
        assert [item[0] for item in seen] == ["computer"] * 5
        assert [item[2] for item in seen] == ["owner-file"] * 5
        assert (files_root / "evie-talk-proof.txt").read_text(encoding="utf-8") == "hi from talk"
    finally:
        live.close()


@pytest.mark.asyncio
async def test_look_and_observe_reroute_to_laptop_files(db_session, files_root: Path) -> None:
    from app.ev.tools import _handle

    target = files_root / "sahaj-file-test.txt"
    target.write_text("hello from sahaj", encoding="utf-8")
    listed = await _handle(
        db_session,
        "look",
        {"prompt": "List the files on my desktop"},
        actor="voice",
    )
    assert listed.get("ok") is True
    assert listed.get("action") == "list" or "sahaj-file-test.txt" in str(listed.get("spoken") or "")
    assert listed.get("ignored") != "system_confirmation"
    read = await _handle(
        db_session,
        "observe_camera",
        {"objective": "Read sahaj-file-test.txt on my desktop"},
        actor="voice",
    )
    assert read.get("ok") is True
    assert "hello from sahaj" in str(read.get("content") or read.get("spoken") or "")
    ignored = await _handle(
        db_session,
        "look",
        {
            "prompt": (
                "(system confirmation — speak this to the owner now) "
                "On Desktop: sahaj-file-test.txt."
            )
        },
        actor="voice",
    )
    assert ignored.get("ignored") == "system_confirmation"
    assert ignored.get("executed") is False


def test_owner_read_edit_open_phrases(files_root: Path) -> None:
    assert looks_like_file_task("look at the files on my desktop")
    write = parse_file_goal(
        "Write a file called sahaj-file-test.txt on my desktop that says hello from sahaj."
    )
    assert write is not None
    assert write["action"] == "write"
    assert str(write["path"]).endswith("sahaj-file-test.txt")
    assert write["content"] == "hello from sahaj"
    read = parse_file_goal("Read sahaj-file-test.txt on my desktop")
    assert read is not None and read["action"] == "read"
    edit = parse_file_goal(
        "Edit sahaj-file-test.txt on my desktop and change hello from sahaj to hi from sahaj"
    )
    assert edit is not None and edit["action"] == "edit"
    opened = parse_file_goal("Open index.html on my desktop")
    assert opened is not None and opened["action"] == "open"
    assert str(opened["path"]).endswith("index.html")


@pytest.mark.asyncio
async def test_live_ws_text_file_op_does_not_deadlock_on_mac_result() -> None:
    """Talk sendText must not hold the WS receive loop across file_op."""

    import asyncio
    import json

    from app.voice.live.events import ComputerRequestEvent
    from app.voice.live.session import LiveSession

    spoken: list[str] = []

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def send_text(self, text: str) -> None:
            raise AssertionError(f"Mini must not receive the file command: {text}")

    live = LiveSession(session_id="owner-file-ws", device_id="mac", backchannel_enabled=False)
    live.grok_voice = _OpenAI()

    async def runner(name: str, args: dict, call_id: str) -> str:
        del name
        result = await live.request_computer(
            "file_op", args, timeout=2, request_id=call_id
        )
        return json.dumps({"ok": result.get("ok"), "spoken": result.get("spoken")})

    live.run_live_tool = runner
    try:
        await live.handle_client(
            {
                "type": "text",
                "text": (
                    "Write a file called evie-ws-proof.txt on my desktop "
                    "that says hello from ws"
                ),
            }
        )
        request = None
        for _ in range(50):
            await asyncio.sleep(0)
            pending: list = []
            while True:
                try:
                    pending.append(live.outbound.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for event in pending:
                if isinstance(event, ComputerRequestEvent):
                    request = event
            if request is not None:
                break
        assert request is not None
        assert request.command == "file_op"
        await live.handle_client(
            {
                "type": "computer_result",
                "request_id": request.request_id,
                "ok": True,
                "verified": True,
                "executed": True,
                "action": "write",
                "spoken": "Wrote evie-ws-proof.txt.",
            }
        )
        task = live._owner_text_task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        assert spoken
        assert "Wrote evie-ws-proof.txt." in spoken[0]
        assert all("did not complete" not in item.lower() for item in spoken)
    finally:
        live.close()


def test_natural_phrasing_followups_search_rename_copy(files_root: Path) -> None:
    assert looks_like_file_task("what's on my desk")
    assert parse_file_goal("what's on my desk")["action"] == "list"
    note = parse_file_goal("drop a note on the desktop that says buy milk")
    assert note is not None and note["action"] == "write"
    assert note["content"] == "buy milk"
    pdfs = parse_file_goal("show me pdfs in downloads")
    assert pdfs is not None and pdfs["action"] == "list"
    assert pdfs.get("kind") == "pdf"
    last = str(files_root / "evie-talk-now.txt")
    assert not looks_like_file_task("read it")
    assert looks_like_file_task("read it", last_path=last)
    read = parse_file_goal("read it", last_path=last)
    assert read is not None and read["action"] == "read"
    assert str(read["path"]).endswith("evie-talk-now.txt")
    opened = parse_file_goal("open that", last_path=last)
    assert opened is not None and opened["action"] == "open"
    added = parse_file_goal("add thanks to it", last_path=last)
    assert added is not None and added["action"] == "append"
    assert "thanks" in (added.get("content") or "").lower()
    renamed = parse_file_goal("rename it to daily-note", last_path=last)
    assert renamed is not None and renamed["action"] == "rename"
    copied = parse_file_goal("copy it to documents", last_path=last)
    assert copied is not None and copied["action"] == "copy"
    found = parse_file_goal("find the resume on my desktop")
    assert found is not None and found["action"] == "search"
    replace = parse_file_goal("change hello from sahaj to hi from sahaj", last_path=last)
    assert replace is not None and replace["action"] == "edit"

    (files_root / "evie-talk-now.txt").write_text("hello from sahaj", encoding="utf-8")
    (files_root / "resume.pdf").write_bytes(b"%PDF-1.4\n")
    listed = perform_local({"action": "list", "path": str(files_root), "kind": "pdf"})
    assert listed["ok"] is True
    assert "resume.pdf" in listed["files"]
    assert "evie-talk-now.txt" not in listed["files"]
    found_run = perform_local(
        {"action": "search", "path": str(files_root), "query": "resume"}
    )
    assert found_run["ok"] is True
    assert "resume.pdf" in (found_run.get("files") or [])
    renamed_run = perform_local(
        {
            "action": "rename",
            "path": str(files_root / "evie-talk-now.txt"),
            "dest": str(files_root / "daily-note.txt"),
        }
    )
    assert renamed_run["ok"] is True
    assert (files_root / "daily-note.txt").read_text(encoding="utf-8") == "hello from sahaj"
    assert not (files_root / "evie-talk-now.txt").exists()
    copied_run = perform_local(
        {
            "action": "copy",
            "path": str(files_root / "daily-note.txt"),
            "dest": str(files_root),
        }
    )
    assert copied_run["ok"] is True
    assert Path(copied_run["path"]).exists()
    assert Path(copied_run["path"]) != files_root / "daily-note.txt"


@pytest.mark.asyncio
async def test_last_file_followup_after_write(db_session, files_root: Path) -> None:
    from app.ev.computer_runtime import ensure_state, reset_computer_states
    from app.ev.tools import _run_computer_goal

    reset_computer_states()
    written = await _run_computer_goal(
        db_session,
        {"goal": "Write a file called follow.txt on my desktop that says hello from sahaj"},
        actor="master",
        live_session_id="talk-follow",
        device_id=None,
    )
    assert written["ok"] is True
    state = ensure_state("talk-follow")
    assert state is not None and str(state.last_file_path or "").endswith("follow.txt")
    read = await _run_computer_goal(
        db_session,
        {"goal": "read it", "last_path": state.last_file_path},
        actor="master",
        live_session_id="talk-follow",
        device_id=None,
    )
    assert read["ok"] is True
    assert "hello from sahaj" in str(read.get("content") or read.get("spoken") or "")


@pytest.mark.asyncio
async def test_append_to_default_note_does_not_spawn_second_file(files_root: Path) -> None:
    from app.ev.laptop_files import run_file_goal

    first = parse_file_goal("drop a note on the desktop that says buy milk")
    assert first is not None
    written = await run_file_goal(first)
    assert written["ok"] is True
    path = Path(written["path"])
    assert path.name == "evie-note.txt"
    assert path.read_text(encoding="utf-8") == "buy milk"
    added = parse_file_goal("add eggs to it", last_path=str(path))
    assert added is not None and added["action"] == "append"
    result = await run_file_goal(added)
    assert result["ok"] is True
    assert Path(result["path"]).name == "evie-note.txt"
    assert path.read_text(encoding="utf-8").replace("\r\n", "\n") == "buy milk\neggs"
    assert not (files_root / "evie-note-2.txt").exists()


def test_content_mutations_are_generic_not_grocery_phrases() -> None:
    current = "alpha\nbravo\ncharlie"
    assert apply_simple_edit(current, "delete everything and just keep bravo") == "bravo"
    assert apply_simple_edit(current, "keep only charlie") == "charlie"
    assert apply_simple_edit(current, "remove alpha") == "bravo\ncharlie"
    assert apply_simple_edit(current, "clear it") == ""
    assert apply_simple_edit("buy milk\neggs\nbread", "delete everything and just keeping the bread") == "bread"
    assert apply_simple_edit("buy milk\neggs\nbread", "delete everything and just keep the breads") == "bread"
    assert apply_simple_edit("red\nblue", "change blue to green") == "red\ngreen"


@pytest.mark.asyncio
async def test_file_mutate_delete_copy_run_keep_only(files_root: Path) -> None:
    from app.ev.desk_names import remember_file
    from app.ev.laptop_files import run_file_goal

    note = files_root / "status.txt"
    note.write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    remember_file(note, content="alpha\nbravo\ncharlie\n", source="write")
    assert looks_like_file_task("delete everything and just keep bravo", last_path=str(note))
    kept = parse_file_goal(
        "delete everything and just keep bravo",
        last_path=str(note),
    )
    assert kept is not None and kept["action"] == "edit"
    result = await run_file_goal(kept)
    assert result["ok"] is True
    assert note.read_text(encoding="utf-8").replace("\r\n", "\n").strip() == "bravo"

    note.write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    remember_file(note, content=note.read_text(encoding="utf-8"), source="write")
    dropped = await run_file_goal(parse_file_goal("remove alpha", last_path=str(note)))
    assert dropped["ok"] is True
    assert "alpha" not in note.read_text(encoding="utf-8")
    assert "bravo" in note.read_text(encoding="utf-8")

    copied = parse_file_goal("duplicate it", last_path=str(note))
    assert copied is not None and copied["action"] == "copy"
    copied_run = await run_file_goal(copied)
    assert copied_run["ok"] is True
    assert Path(copied_run["path"]).exists()
    assert Path(copied_run["path"]) != note

    renamed = parse_file_goal("rename it to daily-status", last_path=str(note))
    assert renamed is not None and renamed["action"] == "rename"
    renamed_run = await run_file_goal(renamed)
    assert renamed_run["ok"] is True
    daily = Path(renamed_run["path"])
    assert daily.name.startswith("daily-status")
    assert daily.exists()

    script = files_root / "say_hi.py"
    script.write_text("print('hello from script')\n", encoding="utf-8")
    ran = parse_file_goal("run it", last_path=str(script))
    assert ran is not None and ran["action"] == "run"
    ran_run = await run_file_goal(ran)
    assert ran_run["ok"] is True
    assert "hello from script" in str(ran_run.get("spoken") or ran_run.get("output") or "")

    moved = parse_file_goal("move it to documents", last_path=str(script))
    assert moved is not None and moved["action"] == "move"
    moved_run = await run_file_goal(moved)
    assert moved_run["ok"] is True
    assert Path(moved_run["path"]).exists()

    target = Path(moved_run["path"])
    deleted = parse_file_goal("delete the file", last_path=str(target))
    assert deleted is not None and deleted["action"] == "delete"
    deleted_run = await run_file_goal(deleted)
    assert deleted_run["ok"] is True
    assert not target.exists()

    still_write = parse_file_goal("drop a note on the desktop that says buy milk")
    assert still_write is not None and still_write["action"] == "write"


@pytest.mark.asyncio
async def test_keep_only_after_append_via_computer_goal(db_session, files_root: Path) -> None:
    from app.ev.computer_runtime import reset_computer_states
    from app.ev.tools import _run_computer_goal

    reset_computer_states()
    await _run_computer_goal(
        db_session,
        {"goal": "Drop a note on the desktop that says buy milk"},
        actor="master",
        live_session_id="talk-mutate",
        device_id=None,
    )
    await _run_computer_goal(
        db_session,
        {"goal": "add eggs to it"},
        actor="master",
        live_session_id="talk-mutate",
        device_id=None,
    )
    await _run_computer_goal(
        db_session,
        {"goal": "add bread to it"},
        actor="master",
        live_session_id="talk-mutate",
        device_id=None,
    )
    kept = await _run_computer_goal(
        db_session,
        {"goal": "delete everything and just keep the bread"},
        actor="master",
        live_session_id="talk-mutate",
        device_id=None,
    )
    assert kept["ok"] is True
    assert kept.get("must_continue") is not True
    body = Path(kept["path"]).read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    assert body == "bread"
    assert "milk" not in body
    assert "eggs" not in body


@pytest.mark.asyncio
async def test_add_eggs_to_it_uses_scene_without_last_path(files_root: Path) -> None:
    """Talk often drops ComputerState last_path; 'it' must still hit the milk note."""

    from app.ev.laptop_files import run_file_goal

    assert looks_like_file_task("add eggs to it")
    assert looks_like_file_task("create a note that says buy milk")
    created = parse_file_goal("create a note that says buy milk")
    assert created is not None and created["action"] == "write"
    assert created["content"] == "buy milk"
    written = await run_file_goal(
        parse_file_goal("drop a note on the desktop that says buy milk")
    )
    assert written["ok"] is True
    path = Path(written["path"])
    added = parse_file_goal("add eggs to it")
    assert added is not None
    assert added["action"] == "append"
    assert Path(added["path"]).name == "evie-note.txt"
    result = await run_file_goal(added)
    assert result["ok"] is True
    assert result.get("must_continue") is not True
    assert path.read_text(encoding="utf-8").replace("\r\n", "\n") == "buy milk\neggs"
    assert not (files_root / "evie-note-2.txt").exists()


@pytest.mark.asyncio
async def test_add_eggs_followup_does_not_retry_computer_ui(db_session, files_root: Path) -> None:
    from app.ev.computer_runtime import ensure_state, reset_computer_states
    from app.ev.tools import _run_computer_goal

    reset_computer_states()
    written = await _run_computer_goal(
        db_session,
        {"goal": "Drop a note on the desktop that says buy milk"},
        actor="master",
        live_session_id="talk-eggs",
        device_id=None,
    )
    assert written["ok"] is True
    state = ensure_state("talk-eggs")
    assert state is not None
    state.last_file_path = None
    added = await _run_computer_goal(
        db_session,
        {"goal": "add eggs to it"},
        actor="master",
        live_session_id="talk-eggs",
        device_id=None,
    )
    assert added["ok"] is True
    assert added.get("must_continue") is not True
    assert Path(added["path"]).name == "evie-note.txt"
    assert Path(added["path"]).read_text(encoding="utf-8").replace("\r\n", "\n") == "buy milk\neggs"


@pytest.mark.asyncio
async def test_add_to_it_without_a_file_does_not_loop_ui(db_session, files_root: Path) -> None:
    from app.ev.computer_runtime import reset_computer_states
    from app.ev.desk_names import reset_desk_names
    from app.ev.tools import _run_computer_goal

    reset_computer_states()
    reset_desk_names()
    result = await _run_computer_goal(
        db_session,
        {"goal": "add eggs to it"},
        actor="master",
        live_session_id="talk-eggs-none",
        device_id=None,
    )
    assert result["ok"] is False
    assert result.get("must_continue") is False
    assert result.get("error") == "file_referent_missing"
    assert result.get("name") not in {"inspect_ui", "ui_action", "app_action", "open_app"}


@pytest.mark.asyncio
async def test_live_transcript_add_eggs_after_note(files_root: Path) -> None:
    import json

    from app.ev.laptop_files import run_file_goal
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        parsed = parse_file_goal(str(args.get("goal") or ""), last_path=args.get("last_path"))
        assert parsed is not None
        result = await run_file_goal(parsed)
        return json.dumps(
            {
                "ok": result.get("ok"),
                "result": result,
                "spoken": result.get("spoken"),
                "must_continue": False,
            }
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-eggs-talk", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    try:
        await live.emit(
            FinalTranscriptEvent(
                at_ms=1,
                text="Drop a note on the desktop that says buy milk",
                provider="openai-realtime",
            )
        )
        live._last_life_action = None
        await live.emit(
            FinalTranscriptEvent(
                at_ms=2,
                text="add eggs to it",
                provider="openai-realtime",
            )
        )
        assert [item[2] for item in seen] == ["owner-file", "owner-file"]
        assert seen[1][1]["goal"] == "add eggs to it"
        note = files_root / "evie-note.txt"
        assert note.read_text(encoding="utf-8").replace("\r\n", "\n") == "buy milk\neggs"
        assert not (files_root / "evie-note-2.txt").exists()
        assert any(
            "eggs" in item.lower() or "added" in item.lower() or "evie-note" in item.lower()
            for item in spoken
        )
    finally:
        live.close()


def test_find_resume_and_open_without_folder(files_root: Path) -> None:
    assert looks_like_file_task("find the resume and open it")
    opened = parse_file_goal("find the resume and open it")
    assert opened is not None
    assert opened["action"] == "open"
    assert "resume" in (opened.get("query") or "").lower()
    (files_root / "Sahaj_Patel_Flagship_Resume.pdf").write_bytes(b"%PDF-1.4\n")
    (files_root / "Sahaj_Patel_Resume.pdf").write_bytes(b"%PDF-1.4\n")
    found = perform_local({"action": "search", "path": str(files_root), "query": "resume"})
    assert found["ok"] is True
    assert Path(found["path"]).name == "Sahaj_Patel_Resume.pdf"
    from app.ev.laptop_files import resolve_existing

    target, _matches, error = resolve_existing("", "resume")
    assert error is None
    assert target is not None
    assert target.name == "Sahaj_Patel_Resume.pdf"

    nested_dir = files_root / "Career"
    nested_dir.mkdir()
    buried = nested_dir / "Sahaj_Patel_Resume.pdf"
    (files_root / "Sahaj_Patel_Resume.pdf").unlink()
    (files_root / "Sahaj_Patel_Flagship_Resume.pdf").unlink()
    buried.write_bytes(b"%PDF-1.4\n")
    (files_root / "Sahaj_Patel_Flagship_Resume.pdf").write_bytes(b"%PDF-1.4\n")
    buried_hit, _matches, buried_error = resolve_existing("", "resume")
    assert buried_error is None
    assert buried_hit is not None
    assert buried_hit.name == "Sahaj_Patel_Resume.pdf"
    assert buried_hit.parent.name == "Career"

    note = files_root / "evie-note.txt"
    note.write_text("buy milk", encoding="utf-8")
    stolen = parse_file_goal(
        "find the resume and open it",
        last_path=str(note),
    )
    assert stolen is not None
    assert stolen["action"] == "open"
    assert "resume" in (stolen.get("query") or "").lower()
    assert "evie-note" not in str(stolen.get("path") or "")
    assert looks_like_file_task("Open Notes") is False
    assert looks_like_file_task("open my resume") is True


@pytest.mark.asyncio
async def test_desk_name_grocery_list_survives_opening_resume(files_root: Path) -> None:
    from app.ev.desk_names import remember_file, resolve_alias
    from app.ev.laptop_files import run_file_goal

    first = parse_file_goal("drop a note on the desktop that says buy milk")
    written = await run_file_goal(first)
    assert written["ok"] is True
    note = Path(written["path"])
    assert resolve_alias("grocery list") == note.resolve()
    resume = files_root / "Sahaj_Patel_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    remember_file(
        resume,
        query="resume",
        goal="find the resume and open it",
        source="open",
    )
    assert looks_like_file_task("add eggs to the grocery list")
    added = parse_file_goal(
        "add eggs to the grocery list",
        last_path=str(resume),
    )
    assert added is not None
    assert added["action"] == "append"
    assert Path(added["path"]).name == "evie-note.txt"
    result = await run_file_goal(added)
    assert result["ok"] is True
    assert Path(result["path"]).name == "evie-note.txt"
    body = note.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert body == "buy milk\neggs"
    named = parse_file_goal("what's on the grocery list", last_path=str(resume))
    assert named is not None and named["action"] == "read"
    assert Path(named["path"]).name == "evie-note.txt"
    bound = parse_file_goal("that's my resume", last_path=str(resume))
    assert bound is not None and bound["action"] == "bind"
    named_resume = await run_file_goal(bound)
    assert named_resume["ok"] is True
    assert resolve_alias("resume") == resume.resolve()


@pytest.mark.asyncio
async def test_desk_twin_packet_fragment_and_landed_file(files_root: Path) -> None:
    """Owner Talk demo (quit/reopen Talk after sidecar reload):

    1. Drop a note on the desktop that says buy milk.
    2. Find the resume and open it.
    3. This belongs to the visa packet.
    4. (Drop I-20.pdf in Downloads — she should offer to file it.)
    5. Yes.
    6. Also eggs.
    7. What's in the visa packet?
    """

    from app.ev.desk_scene import (
        bind_visible_text,
        confirm_land,
        packet_inventory,
        resolve_alias_object,
        scan_landed_files,
        seed_watch_snapshot,
        slot_object,
    )
    from app.ev.laptop_files import run_file_goal

    written = await run_file_goal(
        parse_file_goal("drop a note on the desktop that says buy milk")
    )
    assert written["ok"] is True
    note = Path(written["path"])
    resume = files_root / "Sahaj_Patel_Resume.pdf"
    flagship = files_root / "Sahaj_Patel_Flagship_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    flagship.write_bytes(b"%PDF-1.4\n")
    opened = parse_file_goal("find the resume and open it")
    assert opened is not None
    opened_run = await run_file_goal(opened)
    assert opened_run["ok"] is True
    assert Path(opened_run["path"]).name == "Sahaj_Patel_Resume.pdf"

    packed = parse_file_goal("this belongs to the visa packet", last_path=str(resume))
    assert packed is not None
    assert packed["action"] == "packet_add"
    packed_run = await run_file_goal(packed)
    assert packed_run["ok"] is True
    packet = resolve_alias_object("visa packet")
    assert packet is not None
    member_names = [item.get("name") for item in packet_inventory(packet)]
    assert "Sahaj_Patel_Resume.pdf" in member_names

    seed_watch_snapshot()
    landed = files_root / "Sahaj_I-20.pdf"
    landed.write_bytes(b"%PDF-1.4\n")
    offer = scan_landed_files()
    assert offer is not None
    assert offer.get("offered") is True
    assert "I-20" in str(offer.get("spoken") or "")
    assert "Downloads" not in str(offer.get("spoken") or "")
    confirmed = parse_file_goal("yes")
    assert confirmed is not None and confirmed["action"] == "confirm_land"
    yes_run = await run_file_goal(confirmed)
    assert yes_run["ok"] is True
    member_names = [item.get("name") for item in packet_inventory(resolve_alias_object("visa packet"))]
    assert "Sahaj_I-20.pdf" in member_names

    other = parse_file_goal("no, the other PDF")
    assert other is not None and other["action"] == "scene_other"
    other_run = await run_file_goal(other)
    assert other_run["ok"] is True
    assert "Flagship" in str(other_run.get("path") or other_run.get("spoken") or "")

    eggs = parse_file_goal("also eggs")
    assert eggs is not None
    assert eggs["action"] == "append"
    assert Path(eggs["path"]).name == "evie-note.txt"
    eggs_run = await run_file_goal(eggs)
    assert eggs_run["ok"] is True
    body = note.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "buy milk" in body and "eggs" in body
    assert not (files_root / "evie-note-2.txt").exists()

    listing = parse_file_goal("what's in the visa packet")
    assert listing is not None
    listed = await run_file_goal(listing)
    assert listed["ok"] is True
    spoken = str(listed.get("spoken") or "")
    assert "Sahaj_Patel_Resume.pdf" in spoken
    assert "Sahaj_I-20.pdf" in spoken

    script = parse_file_goal(
        "add that. no the other PDF. put it in visa. also bread.",
        last_path=str(resume),
    )
    assert script is not None
    assert script["action"] in {"scene_turns", "packet_add", "append"}
    script_run = await run_file_goal(script)
    assert script_run["ok"] is True
    assert "bread" in note.read_text(encoding="utf-8")
    assert not (files_root / "evie-note-2.txt").exists()

    bind_visible_text("Preview — Sahaj_Patel_Resume.pdf")
    that = slot_object("that")
    assert that is not None
    assert "Resume" in str(that.get("name") or "")

