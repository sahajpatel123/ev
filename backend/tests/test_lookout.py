"""Surface intelligence: JARVIS/Karen-style window sizes, time-types, lookouts."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from app.ev.hud import HUD_SCHEMAS, validate_hud
from app.ev.interaction import build_strategy
from app.ev.lookout import compose_and_maybe_open, plan_surfaces
from app.ev.tool_select import select_tool
from app.notify.lookouts import list_windows, reset
from app.notify.presence import present_url
from app.utils.text import utcnow


def test_casual_question_does_not_open_glass() -> None:
    plan = plan_surfaces("What did I decide about SQLite?", strategy=build_strategy("What did I decide about SQLite?"))
    assert plan.open is False
    assert plan.windows == []


def test_explicit_show_opens_a_window() -> None:
    plan = plan_surfaces("show me my afternoon on screen")
    assert plan.open is True
    assert plan.explicit is True
    kinds = {window.kind for window in plan.windows}
    assert "horizon" in kinds
    assert all(window.size in {"lookout", "card", "brief", "slate", "canvas", "chip", "ticker", "pip"} for window in plan.windows)


def test_full_hud_stacks_lookouts() -> None:
    plan = plan_surfaces("open the full HUD")
    assert plan.open is True
    kinds = [window.kind for window in plan.windows]
    assert "vitals" in kinds
    assert "radar" in kinds
    assert "horizon" in kinds
    assert len(plan.windows) <= 5
    lookouts = [window for window in plan.windows if window.lookout]
    assert lookouts
    assert all(window.time_type in {"lookout", "linger", "hold", "session", "pulse"} for window in lookouts)


def test_watch_language_opens_radar_lookout() -> None:
    plan = plan_surfaces("keep an eye on the deadline")
    assert plan.open is True
    assert any(window.kind == "radar" and window.lookout for window in plan.windows)


def test_emergency_needed_pulse() -> None:
    strategy = build_strategy("The deploy is broken, live, emergency now!")
    plan = plan_surfaces("The deploy is broken, live, emergency now!", strategy=strategy)
    assert plan.needed is True
    assert plan.open is True
    assert any(window.kind == "pulse" or window.time_type == "pulse" for window in plan.windows)


def test_kind_auto_still_respects_small_talk() -> None:
    strategy = build_strategy("hey")
    plan = plan_surfaces("hey", strategy=strategy)
    assert plan.open is False


def test_present_url_encodes_size_and_time() -> None:
    url = present_url(
        title="Radar",
        body="Two alerts",
        kind="radar",
        size="lookout",
        time_type="lookout",
        placement="upper_right",
        window_id="lookout-radar",
        lookout=True,
    )
    assert url.startswith("ev://present?")
    assert "radar" in url
    assert "lookout" in url
    assert "upper_right" in url


def test_lookout_schema_validates() -> None:
    assert "ev.hud.lookout.v1" in HUD_SCHEMAS
    payload = {
        "schema_version": "ev.hud.lookout.v1",
        "generated_at": utcnow(),
        "open": True,
        "windows": [
            {
                "id": "lookout-vitals",
                "kind": "vitals",
                "size": "lookout",
                "time_type": "lookout",
                "placement": "upper_left",
                "title": "Vitals",
                "body": "Readiness 68",
            }
        ],
        "rationale": "health language",
    }
    name, model = validate_hud(payload)
    assert name == "ev.hud.lookout.v1"
    assert model.open is True


def test_tool_select_lookout_phrases() -> None:
    assert select_tool("keep an eye on the deadline").selected == "present"
    assert select_tool("open the command center").selected == "present"


async def test_compose_endpoint_plans_without_lying(client) -> None:
    reset()
    resp = await client.post(
        "/v1/runtime/lookouts",
        json={"message": "show me my readiness", "open": False},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["opened"] is False
    assert data["plan"]["open"] is True
    kinds = {window["kind"] for window in data["windows"]}
    assert "vitals" in kinds


async def test_compose_and_maybe_open_is_honest_in_pytest(db_session) -> None:
    reset()
    payload = await compose_and_maybe_open(
        db_session,
        message="show me a status card",
        reply="All quiet.",
        explicit=True,
    )
    assert payload["open"] is True
    assert payload["opened"] is False
    assert payload.get("degraded") is True
    assert list_windows()


def test_surface_corpus_smoke_passes() -> None:
    from app.training.surface import evaluate_planner

    report = evaluate_planner()
    assert report["passed"] is True, report["misses"]
    assert report["total"] >= 10


def test_public_harvest_records_licenses() -> None:

    from app.training.surface import CORPUS_PATH

    ledger = json.loads(
        (CORPUS_PATH.parent / "public_sources.json").read_text(encoding="utf-8")
    )
    assert ledger["harvest"] == "one-time"
    assert len(ledger["sources"]) >= 8
    for row in ledger["sources"]:
        assert str(row["url"]).startswith("http")
        assert row["license"]
        assert row["title"]


def test_corpus_covers_defining_mechanics() -> None:
    from app.training.surface import REQUIRED_MECHANICS, corpus_inventory, load_corpus

    inventory = corpus_inventory()
    assert inventory["complete"] is True, (
        inventory["missing"],
        inventory.get("uncited"),
        inventory.get("thin"),
        inventory.get("duplicate_thin"),
    )
    assert not inventory.get("thin")
    assert len(inventory.get("held_out") or []) >= 8
    assert set(inventory["required"]) == set(REQUIRED_MECHANICS)
    examples = {row["id"]: row for row in load_corpus()}
    for row in examples.values():
        assert str(row.get("source_url") or "").startswith("http"), row.get("id")
        assert row.get("license"), row.get("id")
    assert examples["quiet-decision"]["open"] is False
    assert examples["show-afternoon"]["open"] is True
    assert examples["emergency-deploy"]["open"] is True
    assert examples["watch-deadline"]["mechanic"] == "watch"
    assert examples["show-vitals"]["mechanic"] == "vitals"
    assert examples["full-hud"]["mechanic"] == "full_hud"
    assert examples["diagnostics-status"]["mechanic"] == "diagnostics"
    assert examples["brief-me"]["mechanic"] == "briefing"
    assert examples["route"]["mechanic"] == "navigation"
    for ident in ("refuse-drones", "refuse-city-scan", "refuse-hack"):
        assert examples[ident]["open"] is False
        assert examples[ident]["kinds"] == []


def test_evaluate_planner_is_deterministic() -> None:
    from app.training.surface import evaluate_planner

    first = evaluate_planner()
    second = evaluate_planner()
    assert first["total"] == second["total"]
    assert first["hits"] == second["hits"]
    assert first["accuracy"] == second["accuracy"]
    assert first["misses"] == second["misses"]
    assert first["passed"] is True


def test_held_out_paraphrases_hit_real_planner() -> None:
    from app.training.surface import evaluate_planner

    held = evaluate_planner(split="held_out")
    assert held["split"] == "held_out"
    assert held["total"] >= 8
    assert held["passed"] is True, held["misses"]
    assert held["misses"] == []


def test_refuse_weapons_surveillance_and_hacking() -> None:
    from app.ev.lookout import plan_surfaces

    for message in (
        "deploy the drones on that target",
        "scan the city cameras for that stranger's face",
        "hack their phone and show me the feed",
    ):
        plan = plan_surfaces(message)
        assert plan.open is False, message
        assert plan.windows == []


def test_surface_rating_changes_calibration(tmp_path, monkeypatch) -> None:
    from app.training import surface as surface_mod

    monkeypatch.setattr(surface_mod, "_training_dir", lambda: tmp_path)
    surface_mod.record_rating(kind="radar", useful=False, message="too much glass")
    surface_mod.record_rating(kind="radar", useful=False)
    surface_mod.record_rating(kind="radar", useful=False)
    payload = surface_mod.calibrate(actor="test")
    assert payload["version"] >= 1
    assert payload["suppress_kinds"].get("radar", 0) >= 0.5
    assert payload["smoke"]["total"] >= 10


async def test_present_auto_kind_uses_intelligence(client) -> None:
    with patch(
        "app.notify.presence.open_presence",
        new=AsyncMock(
            return_value={
                "ok": True,
                "opened": True,
                "surface": "overlay",
                "url": "ev://present?kind=horizon",
                "via": "helper",
                "windows": [{"id": "lookout-horizon", "kind": "horizon"}],
            }
        ),
    ):
        resp = await client.post(
            "/v1/runtime/present",
            json={
                "title": "Afternoon",
                "body": "Next: walk at 16:00",
                "kind": "auto",
                "message": "show me my afternoon on screen",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["opened"] is True
