"""Federated communication search + latest-state resolution. No giant ingest."""

from __future__ import annotations

from typing import Any

from app.digital.taint import taint_external


def normalize_ref(
    *,
    service: str,
    external_id: str | None,
    timestamp: str | None,
    participants: list[str],
    snippet: str,
    retrieved_at: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ref = {
        "service": service,
        "external_id": external_id,
        "timestamp": timestamp,
        "participants": participants,
        "snippet": (snippet or "")[:280],
        "retrieved_at": retrieved_at,
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
    }
    if extra:
        ref.update(extra)
    return taint_external(ref, source=service, extra={"id": external_id})


def merge_chronological(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def ts(item: dict[str, Any]) -> str:
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        return str(content.get("timestamp") or "")

    return sorted(refs, key=ts)


def latest_state(refs: list[dict[str, Any]], *, topic: str | None = None) -> dict[str, Any]:
    """Do not privilege Gmail over WhatsApp. Chronology + evidence wins."""
    ordered = merge_chronological(refs)
    if topic:
        t = topic.lower()
        filtered = []
        for item in ordered:
            content = item.get("content") if isinstance(item.get("content"), dict) else item
            blob = f"{content.get('snippet') or ''} {' '.join(content.get('participants') or [])}".lower()
            if t in blob:
                filtered.append(item)
        ordered = filtered or ordered
    if not ordered:
        return {"status": "none", "answer": None, "citations": []}
    last = ordered[-1]
    content = last.get("content") if isinstance(last.get("content"), dict) else last
    return {
        "status": "ok",
        "answer": content.get("snippet"),
        "service": content.get("service") or last.get("source"),
        "timestamp": content.get("timestamp"),
        "citations": [
            {
                "service": (i.get("content") or i).get("service") if isinstance(i.get("content"), dict) else i.get("service"),
                "external_id": (i.get("content") or i).get("external_id") if isinstance(i.get("content"), dict) else i.get("external_id"),
                "timestamp": (i.get("content") or i).get("timestamp") if isinstance(i.get("content"), dict) else i.get("timestamp"),
                "snippet": (i.get("content") or i).get("snippet") if isinstance(i.get("content"), dict) else i.get("snippet"),
            }
            for i in ordered[-5:]
        ],
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
    }


def person_brief(
    *,
    person: dict[str, Any],
    messages: list[dict[str, Any]],
    waiting: list[dict[str, Any]],
    calendar: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = latest_state(messages)
    open_wait = [w for w in waiting if w.get("state") == "open"]
    return {
        "person": person.get("name"),
        "why_now": (open_wait[0]["what"] if open_wait else None) or (calendar[0].get("summary") if calendar else "recent communication"),
        "latest": latest.get("answer"),
        "latest_service": latest.get("service"),
        "open_commitments": open_wait,
        "upcoming": calendar[:3],
        "files": [{"name": f.get("name"), "source": f.get("source")} for f in files[:5]],
        "decision_needed": any(w.get("direction") == "ON_OWNER" for w in open_wait),
    }
