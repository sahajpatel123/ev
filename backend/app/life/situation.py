"""Situation Model v0.1 — derived view over canonical state. Never authority."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.life.service import situation_snapshot


async def snapshot(session: AsyncSession, *, actor: str) -> dict:
    return await situation_snapshot(session, actor=actor)


def summarize(snapshot: dict) -> str:
    """Concise spoken/text summary for 'Evie, status.' — no data dump."""
    lines: list[str] = []
    top = snapshot.get("top_focus")
    if top:
        lines.append(
            f"Top priority: {top['title']} ({top['priority'].lower()}, {top['status'].lower()})."
        )
    goals = snapshot.get("active_goals") or []
    if goals:
        titles = ", ".join(g["title"] for g in goals[:3])
        more = f" and {len(goals)-3} more" if len(goals) > 3 else ""
        lines.append(f"Active goals: {titles}{more}.")
    blocked = snapshot.get("blocked_goals") or []
    if blocked:
        lines.append(f"Blocked: {', '.join(b['title'] for b in blocked[:3])}.")
    commitments = snapshot.get("open_commitments") or []
    overdue = snapshot.get("overdue_commitments") or []
    if commitments:
        line = f"{len(commitments)} open commitment(s)"
        if overdue:
            line += f", {len(overdue)} overdue"
        lines.append(line + ".")
    changes = snapshot.get("recent_changes") or []
    if changes:
        latest = changes[0]
        at = str(latest.get("at", ""))[:16].replace("T", " ")
        lines.append(f"Most recent change: {latest['type']} at {at}.")
    if not lines:
        return "Nothing active right now."
    return " ".join(lines)


def changes_since_text(events: list[dict], since: datetime | None) -> str:
    if not events:
        return f"No changes recorded since {since or 'your last check'}."
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    parts = [f"{v}× {k}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])]
    return f"{len(events)} change(s): " + ", ".join(parts[:6]) + "."
