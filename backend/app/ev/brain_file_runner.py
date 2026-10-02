"""Brain runner: Muse Spark 1.3 plans, tests, and runs file-sandbox commands.

Everything is handed to the brain: owner text goes to Muse Spark, Spark
returns a JSON plan ({ops: [{op, args}]}), the runner dry-runs each op,
then executes with confirmation, verifying every receipt. No key or no
network degrades to the deterministic laptop_files parser — honestly
flagged degraded=True, never faked as intelligence.

Entry points used by the API + Mac/iPhone relays:
  plan_with_brain(text) -> (plan, source, degraded)
  run_brain_command(text, origin, confirm, dry_run) -> brain receipt
  test_brain_command(text, origin) -> dry-run + real receipts (the "test those
    commands" lane the owner asked for)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("ev.brain_file_runner")

BRAIN_MODEL = "muse-spark-1.3-contributor"
_ALLOWED_OPS = frozenset({
    "discover", "index", "search", "read", "list",
    "write", "edit", "append", "mkdir",
    "delete", "copy", "move", "rename", "run", "undo",
})

_PLAN_SYSTEM = (
    "You are Evie's file-sandbox planner (Muse Spark 1.3 Contributor). "
    "Return ONLY a JSON object {\"ops\": [{\"op\": ..., \"args\": {...}}]}. "
    "Allowed ops: discover, index, search, read, list, write, edit, append, "
    "mkdir, delete, copy, move, rename, run, undo. "
    "search args: {query, kind?, limit?}. read/list args: {path?, query?}. "
    "write args: {path, content}. edit args: {path, content} (FULL new body). "
    "append args: {path, content}. mkdir args: {path}. "
    "delete/run args: {path}. copy/move/rename args: {path, dest}. "
    "One turn = one goal; use at most 3 ops (search then act). "
    "Never invent file content; use the owner's words for write/edit/append."
)


def _fallback_plan(text: str) -> tuple[dict[str, Any], str, bool]:
    """Deterministic parser fallback. Degraded, but real and testable."""
    try:
        from app.ev import laptop_files

        raw = (text or "").strip()
        lowered = raw.lower()
        if re.search(r"\b(what.*(on|in)|list|show).*\b(desktop|documents|downloads|files?)\b", lowered) and len(raw.split()) < 12:
            return {"ops": [{"op": "discover", "args": {}}]}, "deterministic", True
        if re.search(r"\b(find|search|look.*up|where.*is)\b", lowered):
            needle = _needle_from_text(raw)
            return {"ops": [{"op": "search", "args": {"query": needle or raw[:80]}}]}, "deterministic", True
        parsed = laptop_files.parse_file_goal(raw)
        if isinstance(parsed, dict) and parsed.get("action"):
            action = str(parsed["action"]).lower()
            op_map = {
                "search": "search", "list": "list", "read": "read", "open": "read",
                "write": "write", "edit": "edit", "append": "append",
                "delete": "delete", "rename": "rename", "copy": "copy",
                "move": "move", "run": "run",
            }
            op = op_map.get(action, "search")
            args: dict[str, Any] = {}
            for key in ("path", "query", "content", "dest", "kind"):
                if parsed.get(key) not in (None, ""):
                    args[key] = parsed[key]
            if op == "search" and not args.get("query"):
                args["query"] = _needle_from_text(raw) or raw[:80]
            return {"ops": [{"op": op, "args": args}]}, "deterministic", True
    except Exception as exc:
        logger.debug("brain_file_runner.fallback_failed: %s", exc)
    return {"ops": [{"op": "search", "args": {"query": (text or '')[:80]}}]}, "deterministic", True


def _needle_from_text(text: str) -> str:
    stop = {"find", "search", "look", "for", "my", "the", "a", "an", "up", "me", "please", "evie", "on", "laptop", "file", "files"}
    tokens = [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in stop]
    return " ".join(tokens[:8])


def _sanitize_plan(raw: Any) -> dict[str, Any]:
    ops: list[dict[str, Any]] = []
    items: Any = []
    if isinstance(raw, dict):
        items = raw.get("ops") or raw.get("operations") or []
    elif isinstance(raw, list):
        items = raw
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or item.get("action") or "").strip().lower()
        if op not in _ALLOWED_OPS:
            continue
        args = item.get("args") or item.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        ops.append({"op": op, "args": {str(k): v for k, v in args.items()}})
        if len(ops) >= 3:
            break
    return {"ops": ops}


async def plan_with_brain(text: str) -> tuple[dict[str, Any], str, bool]:
    """Ask the owning text brain for a plan; fall back deterministically.

    Under ``jev_kernel`` JEV chooses among deterministic plan candidates (it
    cannot emit free-form paths or file bodies); otherwise Muse Spark plans.
    A missing brain or unusable plan degrades to the deterministic
    laptop_files parser with ``degraded=True`` — never faked as intelligence.
    """
    raw = (text or "").strip()
    if not raw:
        return {"ops": []}, "deterministic", True
    try:
        from app.gateway.muse import jev_kernel_active
        from app.gateway.roles import (
            chat_via_role,
            note_text_call,
            text_role_available,
            text_role_model,
        )

        if not text_role_available():
            raise RuntimeError("text_brain_unavailable")
        if jev_kernel_active():
            return await _jev_plan(raw)
        from app.contracts import ChatMessage

        messages = [
            ChatMessage(role="system", content=_PLAN_SYSTEM),
            ChatMessage(role="user", content=f"Owner request: {raw[:2000]}"),
        ]
        result = await chat_via_role(messages, reasoning_effort="low")
        content = str(getattr(result, "text", "") or "").strip()
        plan = _parse_plan_json(content)
        clean = _sanitize_plan(plan)
        if clean["ops"]:
            note_text_call(usage=getattr(result, "usage", None))
            return clean, text_role_model(), False
        raise RuntimeError("empty_plan")
    except Exception as exc:
        logger.debug("brain_file_runner.plan_fallback: %s", exc)
        fallback, source, degraded = _fallback_plan(raw)
        return _sanitize_plan(fallback), source, degraded


async def _jev_plan(raw: str) -> tuple[dict[str, Any], str, bool]:
    """JEV picks among deterministic candidates; args are never model-invented."""

    from app.gateway.openrouter_jev import JevQuestion, OpenRouterJevError
    from app.gateway.roles import answer_choice, decide_via_role

    fallback, _, _ = _fallback_plan(raw)
    candidates = [
        op
        for op in (fallback.get("ops") or [])
        if isinstance(op, dict) and str(op.get("op") or "")
    ]
    if not candidates:
        return _sanitize_plan(fallback), "deterministic", True
    criteria: dict[str, str] = {
        "none": "None of the candidate plans matches the owner's request.",
    }
    for op in candidates:
        name = str(op.get("op"))
        args = json.dumps(op.get("args") or {}, ensure_ascii=False, default=str)[:120]
        criteria[name] = f"Use the {name} plan with arguments {args}."
    try:
        call = await decide_via_role(
            {
                "request": raw[:2000],
                "candidates": [
                    {"op": str(op.get("op")), "args": op.get("args") or {}}
                    for op in candidates
                ],
                "instructions": (
                    "Pick the candidate plan that matches the owner's request, or none. "
                    "Do not invent paths or file content."
                ),
            },
            {
                "op": JevQuestion(
                    type="choice",
                    instructions="Which candidate plan should Evie run?",
                    criteria=criteria,
                )
            },
            actor="brain_file_runner",
        )
    except OpenRouterJevError:
        logger.info("jev file-plan decision unavailable")
        return _sanitize_plan(fallback), "deterministic", True
    if call.status != "ok":
        logger.info("jev file-plan decision failed: %s", call.error)
        return _sanitize_plan(fallback), "deterministic", True
    choice = answer_choice(call, "op")
    if choice is None or choice == "none":
        return {"ops": []}, "deterministic", True
    for op in candidates:
        if str(op.get("op")) == choice:
            return _sanitize_plan({"ops": [op]}), "jev", False
    return _sanitize_plan(fallback), "deterministic", True


def _parse_plan_json(content: str) -> Any:
    text = (content or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}
    return {}


async def run_brain_command(
    text: str,
    *,
    origin: str = "api",
    confirm: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Plan with the brain, then dry-run/execute each op via the file sandbox."""
    from app.ev import file_sandbox

    plan, source, degraded = await plan_with_brain(text)
    ops = plan.get("ops") or []
    if not ops:
        return {
            "ok": False, "origin": origin, "model": source, "degraded": degraded,
            "error": "empty_plan", "spoken": "I couldn't turn that into a file action.",
            "ops": [], "receipts": [],
        }
    receipts: list[dict[str, Any]] = []
    all_ok = True
    for item in ops:
        receipt = file_sandbox.execute_op(
            str(item.get("op") or ""), dict(item.get("args") or {}),
            origin=f"brain:{origin}" if not str(origin).startswith("brain") else origin,
            confirm=confirm, dry_run=dry_run,
        )
        receipts.append(receipt)
        if receipt.get("needs_confirm"):
            all_ok = False
            break
        if not receipt.get("ok"):
            all_ok = False
            break
    spoken = _brain_spoken(receipts, dry_run=dry_run)
    return {
        "ok": all_ok, "origin": origin, "model": source, "degraded": degraded,
        "ops": ops, "receipts": receipts, "spoken": spoken,
        "needs_confirm": any(r.get("needs_confirm") for r in receipts),
        "dry_run": dry_run,
    }


async def test_brain_command(text: str, *, origin: str = "api") -> dict[str, Any]:
    """The brain tests first (dry-run), then runs for real when the test passes.

    Returns both receipt sets so Mac + iPhone callers can show "tested, then
    ran" instead of hoping the first mutation worked.
    """
    preview = await run_brain_command(text, origin=origin, confirm=False, dry_run=True)
    tested_ok = bool(preview.get("ok")) or bool(preview.get("needs_confirm"))
    # needs_confirm on dry-run means the plan validated — that IS a passing test.
    real: dict[str, Any] | None = None
    if tested_ok and not preview.get("needs_confirm"):
        # Dry-run passed without needing confirm (read-only plan): run it.
        real = await run_brain_command(text, origin=origin, confirm=True, dry_run=False)
    elif tested_ok and preview.get("needs_confirm"):
        # Mutating plan validated: run with explicit confirm (owner already asked).
        real = await run_brain_command(text, origin=origin, confirm=True, dry_run=False)
    else:
        real = None
    ok = bool(real.get("ok")) if real else bool(preview.get("ok"))
    return {
        "ok": ok, "origin": origin,
        "model": preview.get("model"), "degraded": preview.get("degraded", True),
        "test": preview, "result": real,
        "spoken": (real or preview).get("spoken", ""),
    }


def _brain_spoken(receipts: list[dict[str, Any]], *, dry_run: bool) -> str:
    if not receipts:
        return "I couldn't turn that into a file action."
    last = receipts[-1]
    if last.get("needs_confirm"):
        return str(last.get("spoken") or "Confirm that and I'll do it.")
    if last.get("spoken"):
        prefix = "Test says: " if dry_run else ""
        return prefix + str(last["spoken"])
    if last.get("ok"):
        return "Test passed." if dry_run else "Done."
    return str(last.get("error") or "That didn't work.")
