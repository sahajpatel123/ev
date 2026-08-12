"""Tests for research, maker, HUD/route, guardrails, personality, self-eval, and tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.ev.ev_sense import quiet_hours_active
from app.ev.interaction import assertiveness_level, challenge_evidence_kwargs


async def post_event(client: AsyncClient, text: str, *, event_type: str = "note") -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": event_type, "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def test_research_assistant_flow_with_provenance(client: AsyncClient) -> None:
    resp = await client.post("/v1/research/sessions", json={"question": "Which local embedding model is best?"})
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/research/sessions/{session_id}/notes",
        json={
            "note": "nomic-embed-text is strong for local retrieval.",
            "source_url": "https://example.com/benchmark",
            "source_title": "Local embedding benchmark",
        },
    )
    assert resp.status_code == 201, resp.text
    note = resp.json()
    assert note["session_id"] == session_id
    assert note["source_url"] == "https://example.com/benchmark"

    resp = await client.post(
        f"/v1/research/sessions/{session_id}/conclude",
        json={"conclusion": "Use nomic-embed-text for local embeddings."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "concluded"
    assert resp.json()["conclusion"].startswith("Use nomic-embed-text")

    resp = await client.get(f"/v1/research/sessions/{session_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert len(detail["notes"]) == 1

    resp = await client.get("/v1/memories?memory_type=summary")
    assert resp.status_code == 200
    summaries = resp.json()["memories"]
    conclusion = next(m for m in summaries if (m["payload"] or {}).get("kind") == "research_conclusion")
    assert conclusion["source_type"] == "derived"
    assert len(conclusion["source_events"]) >= 3  # session + note + conclusion events


async def test_maker_companion_state_machine_bom_and_prints(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/projects",
        json={"name": "EV wrist unit", "description": "Wearable companion display", "status": "idea"},
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    resp = await client.get(f"/v1/projects/{project_id}/next-step")
    assert resp.status_code == 200
    assert resp.json()["next_status"] == "planning"

    resp = await client.patch(
        f"/v1/projects/{project_id}/status",
        json={"status": "testing"},
    )
    assert resp.status_code == 400  # idea -> testing is not a valid forward transition

    resp = await client.patch(
        f"/v1/projects/{project_id}/status",
        json={"status": "planning", "current_step": "Finalize BOM"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "planning"

    resp = await client.post(
        f"/v1/projects/{project_id}/bom",
        json={"name": "OLED panel", "qty": 2, "unit": "pcs", "location": "shelf A", "reorder_at": 3, "cost": 18.5},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"/v1/projects/{project_id}/print-jobs",
        json={"name": "wrist-case-v1", "estimated_minutes": 120, "filament_grams": 45},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/print-jobs/{job_id}/status",
        json={"status": "done"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["finished_at"] is not None

    resp = await client.get(f"/v1/projects/{project_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["project"]["status"] == "planning"
    assert len(payload["bom"]) == 1
    assert len(payload["print_jobs"]) == 1
    assert payload["next_step"]["next_status"] == "sourcing"

    # Reorder signal should appear in EV Sense because qty (2) <= reorder_at (3).
    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    reorder = next(p for p in resp.json()["predictions"] if p["kind"] == "maker_reorder")
    assert reorder["tier"] == "mention_later"


async def test_hud_card_and_route_briefing(client: AsyncClient) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await client.post(
        "/v1/alerts/watchlist",
        json={
            "kind": "deadline",
            "value": "Airport pickup",
            "priority": 0.7,
            "metadata": {"date": tomorrow, "location": "Airport", "travel_minutes": 45, "prep": "Bring charger"},
        },
    )

    resp = await client.get("/v1/hud/card")
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["schema_version"] == "ev.hud.card.v1"
    assert card["body"]
    assert "Airport pickup" in card["body"]

    resp = await client.get("/v1/hud/route")
    assert resp.status_code == 200
    route = resp.json()
    assert route["schema_version"] == "ev.hud.route.v1"
    assert route["destination"] == "Airport"
    assert route["leave_by"] is not None
    assert route["travel_time_minutes"] == 45
    assert route["prep_checklist"]


async def test_personality_versioning_and_challenge_ceiling(client: AsyncClient) -> None:
    resp = await client.get("/v1/personality")
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
    assert resp.json()["is_current"] is True

    resp = await client.post(
        "/v1/personality",
        json={
            "directness": 5,
            "humor": 1,
            "formality": 1,
            "technicality": 5,
            "assertiveness": 1,
            "verbosity": 2,
            "proactivity": 3,
            "challenge_level": 1,
            "emotional_style": "brisk",
            "reason_for_change": "User wants a gentler coach",
        },
    )
    assert resp.status_code == 200, resp.text
    profile = resp.json()
    assert profile["version"] == 2
    assert profile["is_current"] is True
    assert profile["assertiveness"] == 1

    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I decided to use SQLite for local testing, and document the choice.")
    resp = await client.post(
        "/v1/interaction/mode",
        json={"message": "I keep re-evaluating SQLite instead of moving on"},
    )
    assert resp.status_code == 200
    strategy = resp.json()["strategy"]
    assert strategy["mode"] == "coaching"
    assert strategy["assertiveness"] <= 1
    assert strategy["challenge"] is False


async def test_isolation_guardrail_and_ev_sense(client: AsyncClient) -> None:
    await post_event(client, "I feel lonely and no one cares about what I'm building.")
    await post_event(client, "I'm alone again tonight, ugh.")

    resp = await client.post("/v1/companionship/scan?window_days=14")
    assert resp.status_code == 200, resp.text
    scan = resp.json()
    assert scan["detected"] is True
    assert scan["confidence"] > 0
    assert scan["recommendation"]
    assert len(scan["evidence_ids"]) >= 1

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200
    guardrail = next(p for p in resp.json()["predictions"] if p["kind"] == "isolation_guardrail")
    assert guardrail["tier"] in ("do_nothing", "mention_later")
    assert guardrail["why_now"]


async def test_response_log_relationship_and_self_evaluation(client: AsyncClient) -> None:
    resp = await client.post("/v1/chat", json={"message": "What should I build next?"})
    assert resp.status_code == 200, resp.text

    resp = await client.get("/v1/evaluations")
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) >= 1
    log_id = logs[0]["id"]

    resp = await client.post(
        f"/v1/evaluations/{log_id}",
        json={"was_useful": True, "followed_recommendation": True, "intervention_appropriate": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["was_useful"] is True

    resp = await client.get("/v1/evaluations/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["total"] >= 1
    assert summary["useful_rate"] == 1.0

    resp = await client.get("/v1/relationship")
    assert resp.status_code == 200
    rel = resp.json()
    assert rel["total_interactions"] >= 1
    assert rel["topics"]


async def test_tool_registry_and_dispatcher(client: AsyncClient) -> None:
    resp = await client.get("/v1/tools")
    assert resp.status_code == 200
    specs = resp.json()
    names = {s["name"] for s in specs}
    assert {
        "search_memory",
        "search_timeline",
        "get_person",
        "get_project",
        "get_goals",
        "get_patterns",
        "calculate",
        "get_health_trends",
        "get_gear_status",
        "get_upcoming_alerts",
        "get_research",
    } <= names

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calculate", "arguments": {"expression": "2 + 3 * 4"}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["result"]["result"] == 14.0

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calculate", "arguments": {"expression": "__import__('os')"}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "Unsupported expression" in resp.json()["error"]

    await post_event(client, "I decided to use SQLite for local testing.")
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "search_memory", "arguments": {"query": "SQLite", "k": 5}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["result"]["count"] >= 1

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "unknown_tool", "arguments": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "Unknown tool" in resp.json()["error"]


async def test_quiet_hours_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "quiet_hours_start", "22:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "08:00")
    assert quiet_hours_active(datetime(2026, 8, 9, 23, 30, tzinfo=UTC)) is True
    assert quiet_hours_active(datetime(2026, 8, 9, 12, 0, tzinfo=UTC)) is False


async def test_attachment_capture_and_download(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/attachments",
        files={"file": ("note.txt", b"hello ev", "text/plain")},
        data={"event_type": "file", "source": "ios", "metadata": '{"app":"notes"}'},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    attachment = payload["attachment"]
    event = payload["event"]
    assert attachment["filename"] == "note.txt"
    assert attachment["sha256"]
    assert event["event_type"] == "file"

    resp = await client.get(f"/v1/attachments/{attachment['id']}")
    assert resp.status_code == 200
    assert resp.content == b"hello ev"
    assert resp.headers["content-type"].startswith("text/plain")

    resp = await client.get("/v1/timeline?event_type=file")
    assert any(e["id"] == event["id"] for e in resp.json()["events"])


async def test_memory_correction_forget_restore(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    resp = await client.get("/v1/decisions")
    decision = resp.json()["memories"][0]

    resp = await client.post(
        f"/v1/memories/{decision['id']}/correct",
        json={"corrected_text": "Decided: use Postgres for local testing.", "reason": "I misspoke"},
    )
    assert resp.status_code == 201, resp.text
    corrected = resp.json()
    assert corrected["is_current"] is True
    assert corrected["version"] == decision["version"] + 1
    assert corrected["reason_for_change"] == "I misspoke"
    assert corrected["payload"]["corrected"] is True

    resp = await client.get(f"/v1/audit/{corrected['id']}")
    versions = resp.json()["versions"]
    assert len(versions) == 2
    assert versions[-1]["id"] == corrected["id"]

    resp = await client.post(
        f"/v1/memories/{corrected['id']}/forget",
        json={"reason": "no longer relevant"},
    )
    assert resp.status_code == 200
    forgotten = resp.json()
    assert forgotten["is_current"] is False
    assert forgotten["payload"]["forgotten"] is True

    resp = await client.post(f"/v1/memories/{corrected['id']}/restore")
    assert resp.status_code == 200
    restored = resp.json()
    assert restored["is_current"] is True
    assert restored["payload"]["forgotten"] is False


async def test_continue_reconstructs_state(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "Now I'm implementing the retrieval ranking algorithm.")

    resp = await client.post("/v1/continue")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["resolved"] is True
    assert "retrieval ranking" in payload["focus"]
    assert len(payload["recent_context"]) >= 2
    assert payload["next_actions"]


async def test_tool_selection_routes_intents(client: AsyncClient) -> None:
    cases = {
        "what's 25 * 4?": "calculate",
        "where is my friend Maya?": "get_person",
        "any deadlines coming up?": "get_upcoming_alerts",
        "how was my sleep this week?": "get_health_trends",
        "show me the EV wrist unit project": "get_project",
        "tell me something from my memory": "search_memory",
    }
    for message, expected in cases.items():
        resp = await client.post("/v1/gateway/select-tool", json={"message": message})
        assert resp.status_code == 200, resp.text
        assert resp.json()["selected"] == expected, message


async def test_chat_sse_stream(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat",
        json={"message": "What did I decide about SQLite?", "stream": True},
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: memory-delta" in body
    assert "event: provenance" in body
    assert "event: delta" in body
    assert "event: done" in body


# --------------------------------------------------------------------------- #
# Evidence-based challenge (L3 requires real evidence, never inferred)
# --------------------------------------------------------------------------- #


def test_l3_challenge_requires_three_reevaluations_in_30_days() -> None:
    assert (
        assertiveness_level(
            evidence_count=5,
            decision_loop_count=3,
            pattern_confidence=0.8,
            recent_reevaluations_30d=3,
            outcome_citations=3,
        )
        == 3
    )
    # Only two re-evaluations in the window: weak evidence, no challenge.
    assert (
        assertiveness_level(
            evidence_count=5,
            decision_loop_count=3,
            pattern_confidence=0.8,
            recent_reevaluations_30d=2,
            outcome_citations=3,
        )
        <= 2
    )


def test_l3_challenge_requires_cited_outcomes() -> None:
    assert (
        assertiveness_level(
            evidence_count=5,
            decision_loop_count=3,
            pattern_confidence=0.8,
            recent_reevaluations_30d=3,
            outcome_citations=2,
        )
        <= 2
    )


def test_l3_challenge_requires_pattern_confidence() -> None:
    assert (
        assertiveness_level(
            evidence_count=5,
            decision_loop_count=3,
            pattern_confidence=0.6,
            recent_reevaluations_30d=3,
            outcome_citations=3,
        )
        <= 2
    )


def test_unknown_evidence_counts_reduce_assertiveness() -> None:
    # Callers that cannot prove the 30-day window or outcomes get no L3.
    assert (
        assertiveness_level(
            evidence_count=5,
            decision_loop_count=3,
            pattern_confidence=0.8,
        )
        <= 2
    )


def test_challenge_evidence_kwargs_uses_real_counts() -> None:
    loops = [
        {"topic": "SQLite", "count": 4, "confidence": 0.9},
        {"topic": "Docker", "count": 2, "confidence": 0.7},
    ]
    outcomes = [
        SimpleNamespace(decision_topic="SQLite"),
        SimpleNamespace(decision_topic="SQLite"),
        SimpleNamespace(decision_topic="SQLite"),
        SimpleNamespace(decision_topic="Docker"),
    ]
    kwargs = challenge_evidence_kwargs(decision_loops=loops, outcomes=outcomes)
    assert kwargs == {"recent_reevaluations_30d": 4, "outcome_citations": 3}


def test_challenge_evidence_kwargs_returns_zero_when_absent() -> None:
    kwargs = challenge_evidence_kwargs(decision_loops=[], outcomes=[])
    assert kwargs == {"recent_reevaluations_30d": 0, "outcome_citations": 0}
