"""Tests for the E.V.-inspired futuristic feature layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.ev_sense import apply_attention_policy
from app.ev.self_eval import log_response, update_evaluation
from app.main import app
from app.schemas import EvaluationUpdate, SensePrediction


async def post_event(
    client: AsyncClient,
    text: str,
    *,
    event_type: str = "note",
    source: str = "test",
    privacy_level: str = "normal",
    occurred_at: str | None = None,
) -> dict:
    body: dict = {
        "source": source,
        "event_type": event_type,
        "text": text,
        "privacy_level": privacy_level,
    }
    if occurred_at is not None:
        body["occurred_at"] = occurred_at
    resp = await client.post(
        "/v1/events",
        json=body,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def find_current_decision(client: AsyncClient, topic_fragment: str = "sqlite") -> dict:
    resp = await client.get("/v1/decisions")
    assert resp.status_code == 200
    decisions = resp.json()["memories"]
    return next(d for d in decisions if topic_fragment.lower() in d["text"].lower())


async def test_auth_rejected_without_token() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/timeline")
    assert resp.status_code == 401


async def test_capture_retrieve_audit_roundtrip(client: AsyncClient) -> None:
    event = await post_event(client, "Remember that I live in Kyoto.")
    assert event["id"]

    resp = await client.get(f"/v1/events/{event['id']}")
    assert resp.status_code == 200
    assert "Kyoto" in resp.json()["content"]["text"]

    resp = await client.get("/v1/timeline")
    assert resp.status_code == 200
    assert any(e["id"] == event["id"] for e in resp.json()["events"])

    resp = await client.get("/v1/memories?q=Kyoto")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1
    memory = resp.json()["memories"][0]

    resp = await client.get(f"/v1/audit/{memory['id']}")
    assert resp.status_code == 200
    audit = resp.json()
    assert audit["memory"]["id"] == memory["id"]
    assert any(e["id"] == event["id"] for e in audit["source_events"])


async def test_decision_versioning_preserves_v1(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I decided to use SQLite for local testing, and document the choice.")

    resp = await client.get("/v1/decisions")
    assert resp.status_code == 200
    assert len(resp.json()["memories"]) == 1
    current = resp.json()["memories"][0]

    resp = await client.get(f"/v1/audit/{current['id']}")
    versions = resp.json()["versions"]
    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[0]["is_current"] is False
    assert versions[1]["version"] == 2
    assert versions[1]["is_current"] is True
    assert versions[1]["supersedes_id"] == versions[0]["id"]
    assert versions[1]["reason_for_change"] == "Value changed"


async def test_decision_outcome_writes_lesson(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    decision = await find_current_decision(client)

    resp = await client.post(
        f"/v1/decisions/{decision['id']}/outcome",
        json={
            "expected_outcome": "faster local testing",
            "actual_outcome": "much slower than Postgres",
            "lesson": None,
        },
    )
    assert resp.status_code == 201, resp.text
    outcome = resp.json()
    assert outcome["status"] == "reviewed"
    assert "Expected: faster local testing" in outcome["lesson"]

    resp = await client.get("/v1/memories?memory_type=lesson")
    lessons = resp.json()["memories"]
    assert any("faster local testing" in m["text"] for m in lessons)


async def test_pattern_engine_detects_research_loop(client: AsyncClient) -> None:
    for _ in range(3):
        await post_event(client, "I decided to compare SQLite and Postgres again.")

    resp = await client.post("/v1/patterns/analyze?window_days=30&min_count=3")
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"]

    resp = await client.get("/v1/patterns")
    patterns = resp.json()["memories"]
    loop = next(p for p in patterns if (p["payload"] or {}).get("kind") == "research_loop")
    assert loop["payload"]["count"] == 3
    assert len(loop["payload"]["evidence"]) == 3
    assert loop["payload"]["first_observed"] <= loop["payload"]["latest_observed"]
    assert 0.5 <= loop["confidence"] <= 0.95


async def test_ev_sense_intervention_gating(client: AsyncClient) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={
            "kind": "deadline",
            "value": "EV demo submission",
            "priority": 0.8,
            "metadata": {"date": tomorrow},
        },
    )
    assert resp.status_code == 201

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    deadline = next(p for p in payload["predictions"] if p["kind"] == "deadline")
    assert deadline["deliver"] is True
    assert deadline["tier"] in ("notify", "notify_card")
    assert deadline["intervention_score"] >= 0.35
    assert deadline["why_now"]
    assert deadline["basis_ids"]

    resp = await client.get("/v1/sense/predictions?status=pending")
    assert resp.status_code == 200
    assert any(p["kind"] == "deadline" for p in resp.json())

    prediction = next(p for p in resp.json() if p["kind"] == "deadline")
    resp = await client.post(
        f"/v1/sense/predictions/{prediction['id']}/outcome",
        json={"outcome": "correct"},
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "correct"
    assert resp.json()["reviewed_at"] is not None


async def test_prediction_flows_into_alert_and_challenge(client: AsyncClient) -> None:
    """Predictions must promote to alert-radar alerts and drive coaching challenges."""
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={
            "kind": "deadline",
            "value": "EV demo submission",
            "priority": 0.8,
            "metadata": {"date": tomorrow},
        },
    )
    assert resp.status_code == 201

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    deadline = next(p for p in resp.json()["predictions"] if p["kind"] == "deadline")
    assert deadline["deliver"] is True
    assert deadline["intervention_score"] >= 0.6

    resp = await client.get("/v1/alerts?status=pending")
    assert resp.status_code == 200
    promoted = next(
        (a for a in resp.json() if a["kind"] == "prediction" and a["source"] == "ev_sense:deadline"),
        None,
    )
    assert promoted is not None
    assert promoted["tier"] == "urgent"
    assert promoted["priority"] >= 0.6
    assert promoted["details"]["prediction_kind"] == "deadline"
    assert promoted["rationale"]

    resp = await client.post(
        "/v1/interaction/mode",
        json={"message": "What should I focus on next?"},
    )
    assert resp.status_code == 200
    strategy = resp.json()["strategy"]
    assert strategy["mode"] == "coaching"
    assert strategy["challenge"] is True
    assert "pending alert" in strategy["rationale"]


async def test_outcomes_feed_relationship_model(client: AsyncClient) -> None:
    """Reviewed prediction outcomes must be visible in relationship understanding."""
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={
            "kind": "deadline",
            "value": "EV demo submission",
            "priority": 0.8,
            "metadata": {"date": tomorrow},
        },
    )
    assert resp.status_code == 201
    await client.post("/v1/sense/predict", json={"window_days": 30})

    resp = await client.get("/v1/sense/predictions?status=pending")
    prediction = next(p for p in resp.json() if p["kind"] == "deadline")
    resp = await client.post(
        f"/v1/sense/predictions/{prediction['id']}/outcome",
        json={"outcome": "correct"},
    )
    assert resp.status_code == 200

    resp = await client.get("/v1/relationship")
    assert resp.status_code == 200
    relationship = resp.json()
    assert relationship["prediction_reviews"] >= 1
    assert relationship["prediction_accuracy"] == 1.0


async def test_calibration_diagnostics(client: AsyncClient) -> None:
    resp = await client.post("/v1/diagnostics/calibrate")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["schema_version"] == "ev.calibration.v1"
    names = {c["name"] for c in report["checks"]}
    assert {"database", "embeddings", "chat_gateway", "retrieval", "object_storage"} <= names
    assert report["overall"] in ("ok", "degraded", "failed")
    assert report["recommendations"]


async def test_tactical_brief_schema(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I decided to use SQLite for local testing, and document the choice.")
    await post_event(client, "I want to build EV as a persistent personal AI.")

    resp = await client.post("/v1/tactical/brief", json={"topic": "SQLite", "stakes": "high"})
    assert resp.status_code == 200, resp.text
    brief = resp.json()
    assert brief["schema_version"] == "ev.hud.briefing.v1"
    assert brief["objective"] == "SQLite"
    assert brief["provenance"]
    assert brief["options"]
    assert any("sqlite" in d["topic"] for d in brief["decision_history"])
    assert brief["talking_points"]


async def test_health_radar_snapshot_trends_and_anomalies(client: AsyncClient) -> None:
    for sleep, hrv, hr, steps in ((7.5, 60, 60, 8000), (7.0, 58, 61, 7500), (7.2, 62, 60, 8200)):
        resp = await client.post(
            "/v1/health/snapshot",
            json={
                "metrics": {
                    "sleep_hours": sleep,
                    "hrv_ms": hrv,
                    "resting_hr": hr,
                    "steps": steps,
                    "mood": 1.0,
                }
            },
        )
        assert resp.status_code == 201, resp.text

    first = resp.json()
    assert first["readiness"] >= 70
    assert first["band"] in ("Good", "Excellent")

    resp = await client.post(
        "/v1/health/snapshot",
        json={
            "metrics": {
                "sleep_hours": 2.0,
                "hrv_ms": 30,
                "resting_hr": 85,
                "steps": 100,
                "mood": -1.0,
            }
        },
    )
    extreme = resp.json()
    assert extreme["readiness"] < 45
    assert any(a["metric"] == "sleep_hours" for a in extreme["anomalies"])

    resp = await client.get("/v1/health/trends?metric=sleep_hours&window_days=14")
    assert resp.status_code == 200
    trend = resp.json()
    assert len(trend["points"]) == 4
    assert trend["baseline_median"] is not None
    assert len(trend["z_scores"]) == 4

    resp = await client.get("/v1/health/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["readiness"] is not None
    assert summary["recommendation"]
    assert summary["anomalies"]


async def test_helio_aliases_and_clinical_emergency(client: AsyncClient) -> None:
    from app.ev.health_radar import clinical_flags, normalize_metrics

    mapped = normalize_metrics({"hr": 72, "hrv": 50, "blood_oxygen": 97, "stress_score": 20})
    assert mapped["heart_rate"] == 72
    assert mapped["hrv_ms"] == 50
    assert mapped["spo2"] == 97
    flags = clinical_flags({"heart_rate": 155, "spo2": 88})
    assert any(row["metric"] == "heart_rate" and row.get("emergency") for row in flags)
    assert any(row["metric"] == "spo2" for row in flags)

    resp = await client.post(
        "/v1/health/snapshot",
        json={"source": "amazfit_helio", "metrics": {"hr": 148, "spo2": 90, "hrv": 12}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "amazfit_helio"
    assert any(flag.get("clinical") for flag in body["anomalies"])


async def test_alert_radar_scan_dedup(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "sqlite", "priority": 0.6},
    )
    assert resp.status_code == 201

    await post_event(client, "I decided to use SQLite for local testing.")

    resp = await client.get("/v1/alerts/scan?window_days=7")
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["scanned_events"] >= 1
    assert len(first["alerts_created"]) >= 1

    resp = await client.get("/v1/alerts/scan?window_days=7")
    second = resp.json()
    assert second["alerts_created"] == []
    assert second["existing_alerts"] >= 1

    resp = await client.get("/v1/alerts?status=pending")
    alerts = resp.json()
    assert alerts
    alert_id = alerts[0]["id"]
    resp = await client.post(f"/v1/alerts/{alert_id}/dismiss", json={"reason": "noise"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


async def test_person_finder_whereabouts(client: AsyncClient) -> None:
    await post_event(client, "Met my friend Maya for coffee.")
    resp = await client.get("/v1/people/Maya/whereabouts")
    assert resp.status_code == 200, resp.text
    info = resp.json()
    assert info["entity_id"] is not None
    assert info["total_events"] >= 1
    assert info["last_seen"] is not None
    assert info["last_seen"]["text"] == "Met my friend Maya for coffee."
    assert info["related_memories"]


async def test_interaction_mode_selection(client: AsyncClient) -> None:
    resp = await client.post("/v1/interaction/mode", json={"message": "Deploy is broken and the server is down, urgent!"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "emergency"
    assert resp.json()["strategy"]["urgency"] >= 0.75

    resp = await client.post(
        "/v1/interaction/mode",
        json={"message": "Should I use Postgres or MongoDB for the memory architecture?"},
    )
    assert resp.json()["mode"] == "analytical"

    resp = await client.post("/v1/interaction/mode", json={"message": "hey"})
    assert resp.json()["mode"] == "casual"

    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I decided to use SQLite for local testing, and document the choice.")
    resp = await client.post(
        "/v1/interaction/mode",
        json={"message": "I keep re-evaluating SQLite instead of moving on"},
    )
    assert resp.json()["mode"] == "coaching"
    assert resp.json()["strategy"]["challenge"] is True


async def test_chat_flow_and_privacy_boundary(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(
        client,
        "Qubit-9 is my secret project codename.",
        privacy_level="never_send_to_model",
    )

    resp = await client.post(
        "/v1/chat",
        json={"message": "Why did I decide to use SQLite for local testing?"},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["reply"]
    assert result["context_tokens"] > 0
    assert result["provenance"]
    assert all("Qubit-9" not in p["text"] for p in result["provenance"])

    resp = await client.get("/v1/timeline?event_type=message.assistant")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) >= 1


async def test_export_and_tombstone_invariants(client: AsyncClient) -> None:
    event = await post_event(client, "I decided to use SQLite for local testing.")
    resp = await client.get("/v1/memories?q=SQLite")
    memory = resp.json()["memories"][0]

    resp = await client.post("/v1/export")
    assert resp.status_code == 200
    bundle = resp.json()
    assert any(e["id"] == event["id"] for e in bundle["events"])
    assert any(m["id"] == memory["id"] for m in bundle["memories"])

    resp = await client.delete(f"/v1/events/{event['id']}?reason=user-requested")
    assert resp.status_code == 200
    assert resp.json()["tombstoned_at"] is not None

    resp = await client.get("/v1/timeline")
    assert all(e["id"] != event["id"] for e in resp.json()["events"])

    resp = await client.get(f"/v1/memories/{memory['id']}")
    assert resp.status_code == 200
    assert resp.json()["redacted"] is True

    resp = await client.get("/v1/events/{id}".replace("{id}", event["id"]))
    assert resp.status_code == 200
    assert resp.json()["tombstoned_at"] is not None


async def test_gear_telemetry(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/gear/snapshot",
        json={
            "device_id": "iphone-16-pro",
            "battery_percent": 87.0,
            "storage_free_bytes": 512_000_000_000,
            "memory_used_percent": 42.0,
            "cpu_percent": 11.0,
            "uptime_seconds": 86400,
        },
    )
    assert resp.status_code == 201, resp.text
    snapshot = resp.json()
    assert snapshot["device_id"] == "iphone-16-pro"
    assert snapshot["battery_percent"] == 87.0

    resp = await client.get("/v1/gear?device_id=iphone-16-pro")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_user_state_engine(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "Blocked on the retrieval ranking algorithm.")
    await post_event(client, "The migration script worked.")
    resp = await client.get("/v1/state")
    assert resp.status_code == 200
    state = resp.json()
    assert state["activity"] == "coding"
    assert state["recent_topics"]
    assert state["open_decisions"]
    assert any("retrieval ranking" in f for f in state["recent_failures"])
    assert any("migration script worked" in s for s in state["recent_successes"])
    assert state["updated_at"] is not None


async def test_user_state_includes_active_focus(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/focus",
        json={"label": "Ship EV demo", "kind": "goal", "reason": "test"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get("/v1/state")
    assert resp.status_code == 200
    assert resp.json()["current_focus"] == "Ship EV demo"


async def test_device_pairing_and_revocation(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/devices",
        json={"name": "iphone-16-pro", "capabilities": ["voice", "camera", "health"]},
    )
    assert resp.status_code == 201, resp.text
    device = resp.json()
    assert device["token"]
    device_id = device["device"]["id"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device['token']}"},
    ) as device_client:
        resp = await device_client.get("/v1/timeline")
        assert resp.status_code == 200

    resp = await client.delete(f"/v1/devices/{device_id}")
    assert resp.status_code == 200
    assert resp.json()["revoked_at"] is not None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device['token']}"},
    ) as revoked_client:
        resp = await revoked_client.get("/v1/timeline")
        assert resp.status_code == 401


async def test_self_eval_calibrates_challenge_and_budget(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Low challenge acceptance must lower the challenge ceiling and alert budget."""
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I decided to use SQLite for local testing, and document the choice.")

    response_ids = []
    for i in range(3):
        row = await log_response(
            db_session,
            request_text=f"Should I keep switching tools? {i}",
            reply_text="A strong recommendation.",
            mode="coaching",
            strategy={"challenge": True},
            provenance_ids=[],
            context_tokens=100,
            model="mock",
        )
        response_ids.append(row.id)
    for response_id in response_ids:
        await update_evaluation(
            db_session,
            response_id,
            EvaluationUpdate(intervention_appropriate=False),
        )
    await db_session.commit()

    resp = await client.get("/v1/calibration/tuning")
    assert resp.status_code == 200
    tuning = resp.json()
    assert tuning["challenge_ceiling"] == 2
    assert tuning["budget_adjustment"] == -1
    assert tuning["daily_budget"] == 4
    assert tuning["challenge_acceptance_rate"] == 0.0
    assert tuning["intervention_appropriate_rate"] == 0.0
    assert "below 40%" in tuning["rationale"]

    resp = await client.post(
        "/v1/interaction/mode",
        json={"message": "I keep re-evaluating SQLite instead of moving on"},
    )
    assert resp.status_code == 200
    strategy = resp.json()["strategy"]
    assert strategy["mode"] == "coaching"
    assert strategy["challenge"] is False
    assert strategy["assertiveness"] <= 2
    assert "calibrated challenge ceiling=2" in strategy["rationale"]


async def test_calibrated_budget_limits_delivery(db_session: AsyncSession) -> None:
    prediction = SensePrediction(
        kind="deadline",
        text="Deadline approaching",
        confidence=0.9,
        intervention_score=0.5,
        why_now="Because a deadline exists",
        basis_ids=["x"],
        tier="notify",
        deliver=True,
    )
    result = await apply_attention_policy(db_session, [prediction], budget_override=0)
    assert result[0].deliver is False
    assert result[0].tier == "mention_later"


async def test_pattern_engine_detects_goal_drift_and_project_abandonment(
    client: AsyncClient,
) -> None:
    """Silent active goals and projects must produce evidence-backed patterns."""
    now = datetime.now(UTC)
    await post_event(
        client,
        "I want to build the EV demo.",
        occurred_at=(now - timedelta(days=20)).isoformat(),
    )
    await post_event(
        client,
        "Working on @ev-demo integration today.",
        occurred_at=(now - timedelta(days=12)).isoformat(),
    )
    await post_event(
        client,
        "Deploying @ev-demo to staging.",
        occurred_at=(now - timedelta(days=9)).isoformat(),
    )

    resp = await client.post("/v1/patterns/analyze?window_days=30&min_count=2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"]

    resp = await client.get("/v1/patterns")
    assert resp.status_code == 200
    patterns = resp.json()["memories"]
    kinds = {(p["payload"] or {}).get("kind") for p in patterns}
    assert "goal_drift" in kinds
    assert "project_abandonment" in kinds

    goal_drift = next(p for p in patterns if (p["payload"] or {}).get("kind") == "goal_drift")
    assert goal_drift["payload"]["silence_days"] >= 7
    assert len(goal_drift["payload"]["evidence"]) >= 2

    abandoned = next(
        p for p in patterns if (p["payload"] or {}).get("kind") == "project_abandonment"
    )
    assert abandoned["payload"]["count"] >= 2
    assert abandoned["payload"]["silence_days"] >= 7

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    pattern_predictions = [p for p in resp.json()["predictions"] if p["kind"] == "pattern"]
    assert any("quiet for" in p["text"] or "hasn't been mentioned" in p["text"] for p in pattern_predictions)
