"""UI-specific verbs — read/see/click/type/... (EV VOICE CONTROL PLAN §4).

Offline-safe: no connected EV.app and no life helper (conftest blanks them),
so executions return the truthful not-connected failures — never fabricated
success. Registry, validation, and surface behavior are tested here.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.tool_select import LIVE_VOICE_TOOLS, SHADOW_VOICE_TOOLS
from app.ev.tools import (
    TOOL_SPECS,
    UI_VERB_MAP,
    _handle,
    dispatch,
    get_spec,
    list_tools,
)

UI_VERBS = frozenset(
    {
        "read",
        "see",
        "click",
        "double_click",
        "right_click",
        "type",
        "paste",
        "key",
        "scroll",
        "drag",
    }
)


def test_ui_verbs_declared_in_registry() -> None:
    names = {spec["name"] for spec in TOOL_SPECS}
    assert names >= UI_VERBS
    assert "recall_history" in names


def test_ui_verbs_spec_shape() -> None:
    for name in UI_VERBS:
        spec = get_spec(name)
        assert spec is not None, name
        assert spec["parameters"]["additionalProperties"] is False, name
        assert spec["permission"] == "apps:act", name
        assert spec["risk_class"] in {"R0", "R1"}, name
        assert spec["provider"] == "computer", name
        # Read-only verbs have nothing to undo; the acting verbs are undoable.
        assert spec["undoable"] is (name not in {"read", "see"}), name


def test_recall_history_spec_shape() -> None:
    spec = get_spec("recall_history")
    assert spec is not None
    assert spec["permission"] == "memory:read"
    assert spec["risk_class"] == "R0"
    assert spec["read_only"] is True
    params = spec["parameters"]["properties"]
    for key in (
        "query",
        "k",
        "time_range",
        "start_date",
        "end_date",
        "memory_type",
        "as_of",
        "chunk_mode",
        "cursor",
    ):
        assert key in params, key
    assert spec["parameters"]["required"] == ["query"]


def test_ui_verbs_on_live_surfaces() -> None:
    assert UI_VERBS <= LIVE_VOICE_TOOLS
    assert UI_VERBS <= SHADOW_VOICE_TOOLS
    assert SHADOW_VOICE_TOOLS <= LIVE_VOICE_TOOLS
    # Shadow rejects the conflated generic memory searches and the raw
    # per-app computer names — the verbs + recall_history replace them.
    assert "search_memory" not in SHADOW_VOICE_TOOLS
    assert "search_timeline" not in SHADOW_VOICE_TOOLS
    assert "inspect_ui" not in SHADOW_VOICE_TOOLS
    assert "ui_action" not in SHADOW_VOICE_TOOLS
    assert "screen_look" not in SHADOW_VOICE_TOOLS
    assert "app_action" in SHADOW_VOICE_TOOLS
    assert {"recall_history", "send_message", "place_call", "evie_turn"} <= SHADOW_VOICE_TOOLS


def test_ui_verb_map_consistency() -> None:
    # Every registered verb maps onto one computer primitive.
    assert set(UI_VERB_MAP) == UI_VERBS
    for canonical, _defaults in UI_VERB_MAP.values():
        assert canonical in {"inspect_ui", "screen_look", "ui_action"}, canonical
    assert UI_VERB_MAP["double_click"][1]["action"] == "double_click"
    assert UI_VERB_MAP["right_click"][1]["action"] == "right_click"
    assert UI_VERB_MAP["drag"][1]["action"] == "drag"


async def test_type_requires_text(db_session: AsyncSession) -> None:
    response = await dispatch(db_session, "type", {}, actor="master")
    assert response.ok is False
    assert "missing required argument" in (response.error or "")


async def test_paste_without_text_is_schema_ok(db_session: AsyncSession) -> None:
    response = await dispatch(db_session, "paste", {}, actor="master")
    assert "missing required argument" not in (response.error or "")


async def test_click_rejects_unknown_action(db_session: AsyncSession) -> None:
    response = await dispatch(
        db_session, "click", {"ref": "e1_1", "action": "hover"}, actor="master"
    )
    assert response.ok is False
    assert "must be one of" in (response.error or "")


async def test_click_rejects_unknown_argument(db_session: AsyncSession) -> None:
    response = await dispatch(
        db_session, "click", {"ref": "e1_1", "surprise": 1}, actor="master"
    )
    assert response.ok is False
    assert "unknown argument" in (response.error or "")


async def test_drag_requires_full_args(db_session: AsyncSession) -> None:
    response = await dispatch(db_session, "drag", {"ref": "e1_1"}, actor="master")
    assert response.ok is False
    assert "missing required argument" in (response.error or "")


async def test_ui_verb_offline_execution_is_truthful(db_session: AsyncSession) -> None:
    """No connected EV.app → verbs return honest not-connected, never success."""
    for name in ("read", "see", "click", "double_click", "right_click", "type", "paste", "key", "scroll", "drag"):
        args = {
            "read": {"query": "Bluetooth"},
            "see": {"target": "active_window"},
            "click": {"ref": "e1_1"},
            "double_click": {"ref": "e1_1"},
            "right_click": {"ref": "e1_1"},
            "type": {"text": "hello"},
            "paste": {"text": "hello"},
            "key": {"keys": "cmd+space"},
            "scroll": {"direction": "down"},
            "drag": {"ref": "e1_1", "frame_id": "f1", "x": 0.5, "y": 0.5},
        }[name]
        payload = await _handle(db_session, name, args, actor="master")
        assert isinstance(payload, dict), name
        assert "ok" in payload, name
        assert payload["ok"] is False, name  # offline — never a fabricated success
        assert "error" in payload, name


async def test_ui_verb_kill_switch(db_session: AsyncSession, monkeypatch) -> None:
    # The live path reads settings freshly through app.config on every call, and
    # other suites (test_prod_isolation) may importlib.reload(app.config) which
    # rebinds app.config.settings to a NEW object. Patching the module attribute
    # keeps this test hermetic regardless of earlier reloads.
    import app.config as config_module

    flipped = config_module.settings.model_copy(update={"ui_verb_tools_enabled": False})
    monkeypatch.setattr(config_module, "settings", flipped)
    assert get_spec("read") is None
    names = {spec["name"] for spec in list_tools()}
    assert "read" not in names
    assert "search_memory" in names  # unrelated tools unaffected
    payload = await _handle(db_session, "read", {"query": "x"}, actor="master")
    assert payload["ok"] is False
    assert payload["error"] == "ui_verbs_disabled"


async def test_recall_history_and_verbs_no_collision(db_session: AsyncSession) -> None:
    """New names must never collide with existing specs (registry uniqueness)."""
    seen: set[str] = set()
    for spec in TOOL_SPECS:
        name = spec["name"]
        assert name not in seen, name
        seen.add(name)