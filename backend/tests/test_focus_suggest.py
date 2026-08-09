"""E.D.I.T.H. focus auto-suggestion tests."""

from __future__ import annotations

from httpx import AsyncClient


async def post_event(client: AsyncClient, text: str) -> None:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text


async def test_focus_suggest_ranks_state_alerts_and_decisions(client: AsyncClient) -> None:
    await post_event(client, "I want to build EV as a persistent personal AI.")
    await post_event(client, "Blocked on the retrieval ranking algorithm.")
    await post_event(client, "I decided to use SQLite for local testing.")
    await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "sqlite", "priority": 0.9},
    )
    await post_event(client, "I decided to use SQLite for local testing.")
    resp = await client.get("/v1/alerts/scan?window_days=7")
    assert resp.status_code == 200, resp.text

    resp = await client.get("/v1/focus/suggest")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    suggestions = payload["suggestions"]
    assert suggestions
    assert payload["generated_at"]

    scores = [s["score"] for s in suggestions]
    assert scores == sorted(scores, reverse=True)
    labels = [s["label"].lower() for s in suggestions]
    assert any("retrieval ranking" in label for label in labels)
    assert any("build ev" in label for label in labels)
    assert any("sqlite" in label for label in labels)
    for s in suggestions:
        assert s["reason"]
        assert s["source"]
        assert s["kind"] in ("task", "project", "person", "topic", "goal")
        assert 0.0 <= s["score"] <= 1.0


async def test_focus_suggest_excludes_active_focus(client: AsyncClient) -> None:
    await post_event(client, "I want to build EV as a persistent personal AI.")
    await post_event(client, "Blocked on the retrieval ranking algorithm.")

    resp = await client.get("/v1/focus/suggest?limit=10")
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert suggestions

    target = suggestions[0]
    resp = await client.post(
        "/v1/focus",
        json={"label": target["label"], "kind": target["kind"], "reason": "suggested"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["active"] is True

    resp = await client.get("/v1/focus/suggest?limit=10")
    labels = [s["label"].lower() for s in resp.json()["suggestions"]]
    assert target["label"].lower() not in labels
