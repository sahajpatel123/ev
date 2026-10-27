"""Integration guarantees: flag-off byte identity (§4.4) + boundary (§4.5).

§4.4 loads the HEAD-original personality modules (my edits are uncommitted)
and proves every prompt-assembly output is byte-identical with the flag off.
§4.5 proves tool args, confirmation helpers, and receipt-derived replies are
identical with the flag on vs off, over representative tool-turn fixtures.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
PERSONALITY_REL = "backend/app/ev/personality.py"
DYNAMIC_REL = "backend/app/ev/dynamic_personality.py"

_MSGS = [
    "hello",
    "roast me",
    "Microservices for my MVP?",
    "the deploy broke again and I changed nothing",
    "did it send?",
    "I'm overwhelmed, too much today",
]


def _head_source(rel: str, tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git required for HEAD-original comparison")
    root = BACKEND.parent
    proc = subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    out = tmp_path / Path(rel).name.replace(".py", "_head.py")
    out.write_text(proc.stdout, encoding="utf-8")
    return out


def _load_head(rel: str, name: str, tmp_path: Path):
    path = _head_source(rel, tmp_path)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_flag_off_constant_matches_head(tmp_path: Path) -> None:
    from app.ev.personality import (
        PERSONALITY_CONTRACT_VERSION,
        SPEECH_STYLE_INSTRUCTIONS,
    )

    head = _load_head(PERSONALITY_REL, "orig_personality_const", tmp_path)
    assert SPEECH_STYLE_INSTRUCTIONS == head.SPEECH_STYLE_INSTRUCTIONS
    assert PERSONALITY_CONTRACT_VERSION == head.PERSONALITY_CONTRACT_VERSION


def test_flag_off_dynamic_functions_match_head(tmp_path: Path) -> None:
    import app.ev.dynamic_personality as current

    head = _load_head(DYNAMIC_REL, "orig_dynamic_fns", tmp_path)
    for msg in _MSGS:
        assert current.turn_guidance(msg) == head.turn_guidance(msg), msg
        assert current.mood_for(msg) == head.mood_for(msg), msg
        assert current.opine_guidance(msg) == head.opine_guidance(msg), msg
        assert current.smalltalk_block(msg) == head.smalltalk_block(msg), msg
        assert current.taste_block() == head.taste_block(), msg
    assert current.turn_guidance("hi", intent="decision", mode="analytical",
                                 urgency=0.8, emotion="stressed") == \
        head.turn_guidance("hi", intent="decision", mode="analytical",
                           urgency=0.8, emotion="stressed")


def test_flag_off_identity_blocks_match_head(tmp_path: Path) -> None:
    from app.ev.personality import identity_block

    head = _load_head(PERSONALITY_REL, "orig_personality_ident", tmp_path)
    for compact in (True, False):
        for name, tag in (("EVIE", "x"), ("EVIE", "the owner's personal AI")):
            assert identity_block(name, tag, compact=compact) == \
                head.identity_block(name, tag, compact=compact)


def test_flag_off_assemblers_match_head_via_module_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.interaction import build_strategy, strategy_block
    from app.ev.turn import ActionReceipt, build_system_prompt, operator_instructions

    head_pers = _load_head(PERSONALITY_REL, "orig_personality_asm", tmp_path)
    head_dyn = _load_head(DYNAMIC_REL, "orig_dynamic_asm", tmp_path)

    current_op = operator_instructions(who="EVIE", source="chat", message="hi")
    current_sys = build_system_prompt(
        identity="ID", who="EVIE", source="chat", working_on="WORKING ON: x",
        context="CTX", briefing=None, receipts=[ActionReceipt(ok=True, name="t")],
    )
    current_strategy = strategy_block(build_strategy("What is next?"))

    monkeypatch.setitem(sys.modules, "app.ev.dynamic_personality", head_dyn)
    monkeypatch.setitem(sys.modules, "app.ev.personality", head_pers)
    try:
        assert operator_instructions(who="EVIE", source="chat", message="hi") == current_op
        assert build_system_prompt(
            identity="ID", who="EVIE", source="chat", working_on="WORKING ON: x",
            context="CTX", briefing=None,
            receipts=[ActionReceipt(ok=True, name="t")]) == current_sys
        assert strategy_block(build_strategy("What is next?")) == current_strategy
    finally:
        monkeypatch.undo()


def test_no_persona_references_in_foreign_assemblers() -> None:
    for rel in (
        "app/ev/turn.py",
        "app/ev/interaction.py",
        "app/filter/pipeline.py",
        "app/voice/live/gemini_live.py",
        "app/voice/live/session.py",
        "app/device_gateway/webrtc_live.py",
        "app/services/tool_loop.py",
        "app/ev/confirm.py",
    ):
        text = (BACKEND / rel).read_text(encoding="utf-8")
        assert "app.persona" not in text, rel
        assert "PERSONA_ENABLED" not in text, rel


def _flag_on_probe(probe: str) -> str:
    backend = BACKEND
    boot = (
        "import os, tempfile; "
        "os.environ['EV_PERSONA_STORE'] = tempfile.mktemp(suffix='.db');"
        "from app.persona import settings; settings.PERSONA_ENABLED = True;"
    )
    proc = subprocess.run(
        [sys.executable, "-c", boot + probe],
        cwd=backend, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def test_boundary_tool_args_receipts_confirmations_identical() -> None:
    from app.contracts import ChatMessage, ToolSpec
    from app.ev.tools import life_success_reply
    from app.ev.turn import ActionReceipt, confirmed_reply
    from app.services.tool_loop import planned_calls_for

    specs = [ToolSpec(name="send_message", description="send",
                      parameters={"type": "object"}, read_only=False)]
    messages = [ChatMessage(role="user", content="send mom the photos")]
    off_calls = planned_calls_for(messages, specs)
    off_success = life_success_reply({"ok": True}, tool_name="send_message")
    off_confirmed = confirmed_reply(
        "maybe", [ActionReceipt(ok=True, name="send_message")])

    import json

    probe = (
        "from app.contracts import ChatMessage, ToolSpec;"
        "from app.ev.tools import life_success_reply;"
        "from app.ev.turn import ActionReceipt, confirmed_reply;"
        "from app.services.tool_loop import planned_calls_for;"
        "import json;"
        "specs=[ToolSpec(name='send_message',description='send',"
        "parameters={'type':'object'},read_only=False)];"
        "msgs=[ChatMessage(role='user',content='send mom the photos')];"
        "print(json.dumps({'calls':repr(planned_calls_for(msgs,specs)),"
        "'ok':life_success_reply({'ok':True},tool_name='send_message'),"
        "'confirmed':confirmed_reply('maybe',[ActionReceipt(ok=True,name=\"send_message\")])}))"
    )
    on = json.loads(_flag_on_probe(probe).strip().splitlines()[-1])
    assert on["calls"] == repr(off_calls)
    assert on["ok"] == off_success
    assert on["confirmed"] == off_confirmed


def test_boundary_no_persona_text_in_tool_payloads(tmp_path) -> None:
    from app.persona.delegation import delegated_turn_block
    from app.persona.store import PersonaStore

    store = PersonaStore(tmp_path / "b.db", key="thread:boundary")
    text = delegated_turn_block("send mom the photos", store=store)
    assert "TURN TONE:" in text
    # The delegation slice is prompt prose, never tool-call JSON.
    assert '"tool"' not in text and "arguments" not in text.lower()


_HOOK_BATTERY = [
    "Great question! Let's dive in and unpack this game-changer. 🎉🎉",
    "Well, well, well. The file is saved. The file is saved.",
    "On it: the deploy broke again. One dry aside, then the work.",
    "No — it failed. timeout. I can retry. Want that?",
]


def test_gap4_filter_hook_inert_when_flag_off() -> None:
    from app.ev.interaction import build_strategy
    from app.filter.output_filter import enforce_persona

    for text in _HOOK_BATTERY:
        _, _, flags = enforce_persona(text, build_strategy("Hi."))
        assert not [f for f in flags if f.name.startswith("persona_")], text


def test_gap4_filter_hook_live_when_flag_on() -> None:
    import json

    probe = (
        "import json;"
        "from app.ev.interaction import build_strategy;"
        "from app.filter.output_filter import enforce_persona;"
        f"battery={_HOOK_BATTERY!r};"
        "st=build_strategy('Hi.');"
        "res=[enforce_persona(t, st) for t in battery];"
        "print(json.dumps({'texts':[r[0] for r in res],"
        "'flags':[[f.name for f in r[2] if f.name.startswith(\"persona_\")] for r in res]}))"
    )
    payload = json.loads(_flag_on_probe(probe).strip().splitlines()[-1])
    assert any(payload["flags"]), payload
    assert "Well, well, well." not in payload["texts"][1], payload["texts"][1]


def test_gap4_hook_markers_present() -> None:
    hook_file = BACKEND / "app" / "filter" / "output_filter.py"
    text = hook_file.read_text(encoding="utf-8")
    start = text.index("# --- persona-hook (CLI 6) ---")
    end = text.index("# --- end persona-hook ---")
    block = text[start:end]
    assert block.count("\n") <= 15
    assert "PERSONA_ENABLED" in block


def test_gap3_receipts_kwarg_inert_when_flag_off() -> None:
    from app.ev.dynamic_personality import turn_guidance
    from app.ev.turn import ActionReceipt, operator_instructions

    failed = (ActionReceipt(name="send", ok=False, error="no route"),)
    assert turn_guidance("did it send?", receipts=failed) == \
        turn_guidance("did it send?")
    # operator_instructions takes no receipts kwarg (GAP-3 landed on
    # turn_guidance only); it must stay deterministic with flag off.
    assert operator_instructions(who="EVIE", source="chat", message="hi") == \
        operator_instructions(who="EVIE", source="chat", message="hi")


def test_gap3_failed_receipt_forces_roast0_when_flag_on() -> None:
    import json

    probe = (
        "import json;"
        "from app.ev.dynamic_personality import turn_guidance;"
        "from app.ev.turn import ActionReceipt;"
        "failed=(ActionReceipt(name='send', ok=False, error='no route'),);"
        "ok=(ActionReceipt(name='send', ok=True, result={'id':'1'}),);"
        "print(json.dumps({'failed': turn_guidance('did it send?', receipts=failed),"
        "'ok': turn_guidance('did it send?', receipts=ok)}))"
    )
    payload = json.loads(_flag_on_probe(probe).strip().splitlines()[-1])
    assert "ROAST=0" in payload["failed"], payload["failed"][:400]
    assert "ROAST=0" not in payload["ok"], payload["ok"][:400]

