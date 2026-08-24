"""TurnController (G1.3) — Luna interprets, Evie Core owns truth.

Flow:
  owner_turn (final transcript, owner speech only)
    → Luna TurnIntent (typed, validated)
    → Evie Core service (deterministic, with policy/events)
    → TurnResult (authoritative, Realtime may make it sound natural but not contradict)
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.luna_adapter import classify_intent
from app.ev.turn_intent import TurnIntent, TurnResult


class TurnController:
    """Backend service — model-independent above the adapter layer."""

    def __init__(self, session: AsyncSession, *, actor: str = "master", device_id: str | None = None, session_id: str | None = None):
        self.session = session
        self.actor = actor
        self.device_id = device_id
        self.session_id = session_id

    async def handle_turn(self, owner_turn: str, *, turn_id: str | None = None, context: dict | None = None) -> TurnResult:
        """Main entry: owner_turn is canonical final transcript (owner speech only).
        
        If turn_id is provided, it is preferred as reference to canonical transcript;
        backend resolves it. If only owner_turn string is given, it is used directly.
        """
        start = time.perf_counter()
        # Resolve turn_id -> canonical transcript if provided
        canonical_turn = owner_turn
        if turn_id:
            resolved = await self._resolve_turn_transcript(turn_id)
            if resolved:
                canonical_turn = resolved

        # Luna classification — bounded context, cache-friendly prompt
        # Provide minimal context: known projects, current focus, capabilities
        if context is None:
            context = await self._build_luna_context()

        try:
            intent = await classify_intent(canonical_turn, context)
        except Exception as e:
            return TurnResult(
                ok=False, route="UNSUPPORTED", operation="UNKNOWN",
                error=f"intent_classify_failed: {e}",
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        # Route to Evie Core
        try:
            result = await self._route_intent(intent, canonical_turn)
            result.latency_ms = (time.perf_counter() - start) * 1000
            return result
        except Exception as e:
            return TurnResult(
                ok=False, route=intent.route, operation=intent.operation,
                error=f"route_failed: {e}",
                latency_ms=(time.perf_counter() - start) * 1000,
            )

    async def _resolve_turn_transcript(self, turn_id: str) -> str | None:
        """Resolve turn_id to canonical transcript from durable Event or voice_memory.
        
        Currently looks up Event with metadata live_session_id or transcript_source.
        If not found, returns None and caller falls back to owner_turn string.
        """
        # Try Event table: message.user events with that id or content
        try:

            from app.models import Event
            # turn_id may be Event.id or provider item_id
            row = await self.session.get(Event, turn_id) if len(turn_id) > 20 else None
            if row is None:
                # Canonical transcript lookup by provider item id is not yet
                # indexed; fall through and let caller use the literal text.
                return None
        except Exception:
            pass
        return None

    async def _build_luna_context(self) -> dict:
        """Bounded context for Luna — not full memory."""
        ctx: dict[str, Any] = {}
        try:
            from app.life.service import list_projects
            projects = await list_projects(self.session, actor=self.actor, active_only=False)
            ctx["project_titles"] = [p["title"] for p in projects[:10]]
            # Current focus: top project
            if projects:
                # Sort by priority already in list_projects
                top = projects[0] if projects else None
                if top:
                    ctx["current_project"] = top["title"]
            # Capability summary
            ctx["capability_summary"] = "Projects, Goals, Commitments, Mission Control"
        except Exception:
            pass
        return ctx

    async def _route_intent(self, intent: TurnIntent, owner_turn: str) -> TurnResult:
        """Deterministic routing to Evie Core services — Luna never decides DB truth."""
        route, op = intent.route, intent.operation

        # CLARIFICATION early exit
        if intent.needs_clarification or route == "CLARIFICATION":
            return TurnResult(
                ok=True, route="CLARIFICATION", operation="UNKNOWN",
                needs_clarification=True,
                clarification_question=intent.clarification_question or "Which project?",
                owner_message=intent.clarification_question or "Which project?",
            )

        if route == "CONVERSATION":
            return TurnResult(ok=True, route="CONVERSATION", operation="UNKNOWN", owner_message=None)

        if route == "UNSUPPORTED":
            return TurnResult(ok=False, route="UNSUPPORTED", operation="UNKNOWN", error="unsupported", owner_message="I can't do that yet.")

        # STATE_QUERY / STATE_MUTATION
        if route in ("STATE_QUERY", "STATE_MUTATION", "MISSION_CONTROL"):
            return await self._handle_state(intent, owner_turn)

        if route in ("DELEGATED_JOB", "RESEARCH_MISSION"):
            # G1.3: scaffolded, not active. Validate classification, return placeholder.
            return TurnResult(
                ok=True, route=route, operation=op,
                canonical_data={"stub": "manager_not_active", "owner_turn": owner_turn},
                owner_message="That kind of deep work will be handled by my manager soon — for now I can help with your projects, goals, and commitments.",
                error=None,
            )

        if route == "ACTION":
            return TurnResult(ok=True, route="ACTION", operation=op, canonical_data={"stub": "action_via_existing_tools"}, owner_message=None)

        return TurnResult(ok=False, route=route, operation=op, error="unknown_route")

    async def _handle_state(self, intent: TurnIntent, owner_turn: str) -> TurnResult:
        from app.life import service as life
        from app.life.situation import snapshot, summarize

        route, op = intent.route, intent.operation

        # MISSION_CONTROL
        if route == "MISSION_CONTROL" or op in ("STATUS", "WHAT_CHANGED"):
            if op == "WHAT_CHANGED" or "what changed" in owner_turn.lower():
                # WHAT_CHANGED via TurnController — same semantics as mission_control tool
                from datetime import timedelta

                from app.life.service import changes_since, checkpoint, last_checkpoint
                from app.life.situation import changes_since_text
                from app.utils.text import utcnow
                # Determine since: explicit intent.due_at/since? For now use last_checkpoint fallback
                since = await last_checkpoint(self.session, actor=self.actor)
                if since is None:
                    since = utcnow() - timedelta(hours=24)
                changes = await changes_since(self.session, actor=self.actor, since=since)
                # Advance checkpoint on successful WHAT_CHANGED (without explicit since)
                await checkpoint(self.session, actor=self.actor, device_id=self.device_id)
                snap = await snapshot(self.session, actor=self.actor)
                msg = changes_since_text(changes, since)
                # Also include snapshot summary for spoken response
                msg = f"{summarize(snap)} {msg}"
                return TurnResult(
                    ok=True, route="MISSION_CONTROL", operation="WHAT_CHANGED",
                    canonical_data={"changes": changes, "snapshot": snap},
                    owner_message=msg,
                )
            else:
                snap = await snapshot(self.session, actor=self.actor)
                msg = summarize(snap)
                return TurnResult(
                    ok=True, route="MISSION_CONTROL", operation="STATUS",
                    canonical_data={"snapshot": snap},
                    owner_message=msg,
                )

        # PROJECT_*
        if op.startswith("PROJECT"):
            if op == "PROJECT_LIST":
                projects = await life.list_projects(self.session, actor=self.actor, active_only=False)
                if intent.project_title:
                    q = intent.project_title.lower()
                    projects = [p for p in projects if q in p["title"].lower()]
                msg = f"You have {len(projects)} project(s): " + ", ".join(p["title"] for p in projects) if projects else "You have no projects yet."
                return TurnResult(ok=True, route=route, operation=op, canonical_data=projects, owner_message=msg)
            if op == "PROJECT_GET":
                title = intent.project_title or intent.description or ""
                proj = await life.find_project(self.session, actor=self.actor, query=title) if title else None
                if proj:
                    data = {"id": str(proj.id), "title": proj.title, "priority": proj.priority, "status": proj.status}
                    return TurnResult(ok=True, route=route, operation=op, canonical_data=data, owner_message=f"{proj.title} is {proj.priority.lower()} priority, {proj.status.lower()}.")
                # Try list fallback
                projects = await life.list_projects(self.session, actor=self.actor, active_only=False)
                if intent.project_title:
                    q = intent.project_title.lower()
                    matches = [p for p in projects if q in p["title"].lower()]
                    if matches:
                        p = matches[0]
                        return TurnResult(ok=True, route=route, operation=op, canonical_data=p, owner_message=f"{p['title']} is {p['priority'].lower()} priority.")
                return TurnResult(ok=False, route=route, operation=op, error="not_found", owner_message=f"I couldn't find a project called {title}.")
            if op == "PROJECT_CREATE":
                title = intent.description or intent.project_title or owner_turn
                # Extract title better: for "create a project called X", description is X
                if not title or len(title) < 2:
                    return TurnResult(ok=False, route=route, operation=op, error="missing_title")
                res = await life.create_project(self.session, actor=self.actor, title=title, priority=intent.priority or "NORMAL", device_id=self.device_id)
                ok = res.get("ok")
                return TurnResult(ok=bool(ok), route=route, operation=op, canonical_data=res.get("project"), owner_message=res.get("spoken") or (f"Created project {title}." if ok else f"Failed: {res.get('error')}"), error=None if ok else res.get("error"))
            if op == "PROJECT_UPDATE":
                # Needs disambiguation - intent should have project_title and priority/status
                title = intent.project_title or ""
                proj = await life.find_project(self.session, actor=self.actor, query=title) if title else None
                if not proj:
                    return TurnResult(ok=False, route=route, operation=op, error="not_found", needs_clarification=True, clarification_question="Which project?")
                res = await life.update_project(self.session, actor=self.actor, project_id=str(proj.id), priority=intent.priority, device_id=self.device_id)
                ok = res.get("ok")
                return TurnResult(ok=bool(ok), route=route, operation=op, canonical_data=res.get("project"), owner_message=res.get("spoken") or str(res), error=None if ok else res.get("error"))

        # GOAL_*
        if op.startswith("GOAL"):
            if op == "GOAL_LIST":
                # Resolve project filter if present
                project_id = None
                if intent.project_title:
                    proj = await life.find_project(self.session, actor=self.actor, query=intent.project_title)
                    if proj:
                        project_id = str(proj.id)
                    else:
                        return TurnResult(ok=True, route=route, operation=op, canonical_data=[], owner_message=f"No project called {intent.project_title} found.")
                goals = await life.list_goals(self.session, actor=self.actor, project_id=project_id)
                # Title filter if goal_title present and not project context
                if intent.goal_title and not intent.project_title:
                    q = intent.goal_title.lower()
                    goals = [g for g in goals if q in g["title"].lower()]
                msg = f"You have {len(goals)} goal(s)" + (f" in {intent.project_title}" if intent.project_title else "") + (": " + ", ".join(g["title"] for g in goals[:3]) if goals else ".")
                return TurnResult(ok=True, route=route, operation=op, canonical_data=goals, owner_message=msg)
            if op == "GOAL_GET":
                # Find by goal_title substring
                q = (intent.goal_title or intent.description or "").lower()
                goals = await life.list_goals(self.session, actor=self.actor)
                matches = [g for g in goals if q in g["title"].lower()] if q else []
                if matches:
                    full = await life.get_goal(self.session, actor=self.actor, goal_id=matches[0]["id"])
                    return TurnResult(ok=True, route=route, operation=op, canonical_data=full.get("goal"), owner_message=matches[0]["title"])
                return TurnResult(ok=False, route=route, operation=op, error="not_found")
            if op == "GOAL_CREATE":
                title = intent.goal_title or intent.description or ""
                proj_ref = intent.project_title
                res = await life.create_goal(self.session, actor=self.actor, title=title, project_ref=proj_ref, priority=intent.priority or "NORMAL", device_id=self.device_id)
                ok = res.get("ok")
                return TurnResult(ok=bool(ok), route=route, operation=op, canonical_data=res.get("goal"), owner_message=f"Created goal: {title}." if ok else str(res.get("error")), error=None if ok else res.get("error"))
            if op == "GOAL_UPDATE":
                # Find goal by title
                q = intent.goal_title or ""
                goals = await life.list_goals(self.session, actor=self.actor)
                gid = None
                for g in goals:
                    if q.lower() in g["title"].lower():
                        gid = g["id"]
                        break
                if not gid and intent.goal_title:
                    # Try direct id
                    gid = intent.goal_title
                if not gid:
                    return TurnResult(ok=False, route=route, operation=op, error="not_found")
                res = await life.update_goal(self.session, actor=self.actor, goal_id=gid, state=intent.status, priority=intent.priority, device_id=self.device_id)
                return TurnResult(ok=bool(res.get("ok")), route=route, operation=op, canonical_data=res.get("goal"), error=res.get("error"))

        # COMMITMENT_*
        if op.startswith("COMMITMENT"):
            if op in ("COMMITMENT_LIST", "COMMITMENT_GET"):
                q = intent.commitment_query or intent.description or ""
                from app.life.service import list_commitments
                commitments = await list_commitments(self.session, actor=self.actor, open_only=False)
                # Filter by query substring if provided
                if q:
                    ql = q.lower()
                    commitments = [c for c in commitments if ql in c["description"].lower()]
                # Filter by project if needed
                if intent.project_title:
                    proj = await life.find_project(self.session, actor=self.actor, query=intent.project_title)
                    if proj:
                        commitments = [c for c in commitments if c.get("project_id") == str(proj.id)]
                # Read-after-cancel law: a CANCELLED match must never be
                # presented as an active/open commitment.
                if q and len(commitments) == 1 and commitments[0].get("status") == "CANCELLED":
                    c = commitments[0]
                    return TurnResult(
                        ok=True, route=route, operation=op, canonical_data=commitments,
                        owner_message="That commitment was cancelled.",
                    )
                open_ones = [c for c in commitments if c.get("status") == "OPEN"]
                cancelled_n = len(commitments) - len(open_ones)
                msg = f"You have {len(open_ones)} open commitment(s)" + (f" matching {q}" if q else "")
                if open_ones:
                    msg += ": " + "; ".join(c["description"][:60] for c in open_ones[:2])
                    if open_ones[0].get("due_at"):
                        msg += f". Due {open_ones[0]['due_at']}"
                    msg += "."
                elif q:
                    msg += "."
                if cancelled_n:
                    msg += f" ({cancelled_n} matching commitment(s) were cancelled.)"
                return TurnResult(ok=True, route=route, operation=op, canonical_data=commitments, owner_message=msg)
            if op == "COMMITMENT_CREATE":
                desc = intent.description or intent.commitment_query or owner_turn
                due_raw = intent.due_at or ""
                # Fallback to parsing owner_turn if due_at not extracted
                due_str = due_raw or owner_turn
                from app.ev.resolve import parse_owner_when
                due = parse_owner_when(due_str) if due_str else None
                if due is None and desc:
                    due = parse_owner_when(desc)
                res = await life.create_commitment(self.session, actor=self.actor, description=desc, due_at=due, project_ref=intent.project_title, device_id=self.device_id)
                ok = res.get("ok")
                cm = res.get("commitment", {})
                # Post-write verification: the row must be readable as OPEN.
                verified = False
                if ok and cm.get("id"):
                    open_now = await life.list_commitments(self.session, actor=self.actor, open_only=True)
                    verified = any(c["id"] == cm["id"] for c in open_now)
                    if not verified:
                        return TurnResult(ok=False, route=route, operation=op, error="verification_failed")
                due_msg = f" due {cm.get('due_at')}" if cm.get("due_at") else ""
                return TurnResult(ok=bool(ok and verified), route=route, operation=op, canonical_data=cm, owner_message=f"Saved commitment: {desc}{due_msg}." if ok else str(res.get("error")), error=None if ok else res.get("error"))
            if op == "COMMITMENT_UPDATE":
                # Find by commitment_query or description
                q = intent.commitment_query or intent.description or ""
                from app.life.service import list_commitments
                commitments = await list_commitments(self.session, actor=self.actor, open_only=False)
                cid = None
                if q:
                    for c in commitments:
                        if q.lower() in c["description"].lower():
                            cid = c["id"]
                            break
                if not cid:
                    return TurnResult(ok=False, route=route, operation=op, error="not_found")
                res = await life.update_commitment(self.session, actor=self.actor, commitment_id=cid, status=intent.status or "FULFILLED", device_id=self.device_id)
                return TurnResult(ok=bool(res.get("ok")), route=route, operation=op, canonical_data=res.get("commitment"), error=res.get("error"))
            if op == "COMMITMENT_CANCEL":
                # "Delete my X commitment" == semantic CANCEL. Row + history are
                # preserved; only status transitions OPEN -> CANCELLED.
                from app.life.service import list_commitments
                commitments = await list_commitments(self.session, actor=self.actor, open_only=True)
                q = (intent.commitment_query or "").lower().strip()
                matches = [c for c in commitments if not q or q in c["description"].lower()]
                if not matches:
                    return TurnResult(
                        ok=False, route=route, operation=op, error="not_found",
                        owner_message=f"I couldn't find an open commitment{' matching ' + q if q else ''}.",
                    )
                if len(matches) > 1:
                    # Multiple open commitments match: ASK. Never guess which
                    # one the owner means (project-head law).
                    names = "; ".join(c["description"][:40] for c in matches[:3])
                    return TurnResult(
                        ok=True, route="CLARIFICATION", operation=op,
                        needs_clarification=True,
                        clarification_question=f"Which commitment? I found: {names}.",
                        owner_message=f"Which commitment? I found: {names}.",
                        canonical_data=matches,
                    )
                target = matches[0]
                res = await life.update_commitment(
                    self.session, actor=self.actor, commitment_id=target["id"],
                    status="CANCELLED", device_id=self.device_id,
                )
                ok = bool(res.get("ok"))
                # Post-write verification: the cancelled commitment must no
                # longer appear among OPEN commitments.
                open_after = await life.list_commitments(self.session, actor=self.actor, open_only=True)
                verified = all(c["id"] != target["id"] for c in open_after)
                if ok and not verified:
                    return TurnResult(ok=False, route=route, operation=op, error="verification_failed")
                return TurnResult(
                    ok=ok, route=route, operation=op,
                    canonical_data=res.get("commitment"),
                    owner_message=(
                        f"Cancelled your commitment: {target['description'][:60]}."
                        if ok and verified else str(res.get("error") or "cancel_failed")
                    ),
                    error=None if ok else res.get("error"),
                )

        return TurnResult(ok=False, route=route, operation=op, error="not_implemented", owner_message="That operation isn't implemented yet.")
