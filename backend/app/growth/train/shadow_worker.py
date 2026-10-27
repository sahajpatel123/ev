"""Shadow worker: run turns plan-only inside the TARGET tree's code.

Subprocess entry only (never imported by the driver process): installs the
shadow guard on this tree's kernel/executor/fabric references, forces
prepare_only on a scratch cognition, replays turn utterances through the
real loop with the real brain, and writes a per-turn report.

Exits nonzero (no report) if the guard self-test fails: a shadow that
cannot prove its patch points refuses to run rather than risk effects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Lanes whose EXECUTED decision is a read (everything else EXECUTED = leak).
READ_EXECUTED = {("dispatch", "world_query"), ("dispatch", "atlas_discover")}


def _redact(text: str) -> str:
    try:
        from app.security.boundary import strip_for_model
        return str(strip_for_model(text or "")[:2000])
    except Exception:
        return (text or "")[:2000]


def install_guard() -> tuple[bool, str, object]:
    """Patch this tree's kernel/executor/fabric references. Returns (ok, detail, log)."""
    from app.growth.train import shadow_guard as G

    log = G.ShadowLog()
    try:
        import app.cognitive.executor as E
        import app.cognitive.kernel as K
        import app.fabric.dispatch_tools as D
    except Exception as e:
        return False, f"imports failed: {type(e).__name__}: {e}", log
    for mod in (E, K):
        if not hasattr(mod, "execute_semantic"):
            return False, f"{mod.__name__} has no execute_semantic (refactor moved it?)", log
    if not hasattr(D, "run_fabric_tool"):
        return False, "dispatch_tools has no run_fabric_tool (refactor moved it?)", log
    E.execute_semantic = G.wrap_execute_semantic(E.execute_semantic, log)
    K.execute_semantic = G.wrap_execute_semantic(K.execute_semantic, log)
    D.run_fabric_tool = G.wrap_fabric_tool(D.run_fabric_tool, log)
    return True, "guard installed on executor+kernel+fabric", log


async def self_test(log) -> tuple[bool, str]:
    """Fire blocked tools through the kernel's ACTUAL references; all must block."""
    import app.cognitive.kernel as K
    import app.fabric.dispatch_tools as D

    results = []
    try:
        r1 = await K.execute_semantic(None, "agent.run", {"goal": "pwn"},  # type: ignore[arg-type]
                                      cognition=None, actor="s", live_session_id=None,
                                      steering_seen=0)
        results.append(("kernel.execute_semantic/agent.run", r1))
    except Exception as e:
        results.append(("kernel.execute_semantic/agent.run", {"error": type(e).__name__}))
    try:
        r2 = await D.run_fabric_tool(None, "skills_act", {"op": "run", "name": "x"})  # type: ignore[arg-type]
        results.append(("run_fabric_tool/skills_act", r2))
    except Exception as e:
        results.append(("run_fabric_tool/skills_act", {"error": type(e).__name__}))
    for where, r in results:
        if not (isinstance(r, dict) and r.get("shadow_blocked")):
            return False, f"SELF-TEST FAILED at {where}: {str(r)[:200]}"
    return True, "self-test: blocked tools blocked through kernel+fabric refs"


async def run_turns(turns: list[dict], budget: float, tmp: Path) -> dict:
    from app.cognitive import session_store
    from app.cognitive.kernel import handle_turn
    from app.db import Base, make_engine

    # Scratch cognition, forced plan-only.
    cog = session_store.current()
    cog.prepare_only = True
    session_store.save(cog)
    engine = make_engine(f"sqlite+aiosqlite:///{tmp}/shadow.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.fabric import audit as A
    audit_path = Path(str(A._path()))
    per_turn = []
    spent = 0.0
    for idx, t in enumerate(turns):
        if spent >= budget and budget > 0:
            per_turn.append({"idx": idx, "completed": False, "error": "shadow budget exhausted",
                             "calls": [], "audit": [], "kind": "budget"})
            continue
        before_lines = len(audit_path.read_text().splitlines()) if audit_path.is_file() else 0
        usage_before = _key_usage()
        try:
            async with maker() as session:
                res = await handle_turn(transcript=t["owner"], modality="typed",
                                        session=session, actor="master",
                                        persist_memory=False)
            completed = not bool(getattr(res, "unavailable", False) or getattr(res, "error", ""))
            answer, kind, error = _redact(getattr(res, "spoken", "") or ""), str(getattr(res, "kind", "")), str(getattr(res, "error", "") or "")
        except Exception as e:  # noqa: BLE001 - turn failure is comparison data
            completed, answer, kind, error = False, "", "crashed", f"{type(e).__name__}: {e}"[:300]
        usage_after = _key_usage()
        if usage_before is not None and usage_after is not None:
            spent += max(0.0, usage_after - usage_before)
        audit_slice = []
        if audit_path.is_file():
            for line in audit_path.read_text().splitlines()[before_lines:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                audit_slice.append({"lane": e.get("lane"), "op": e.get("op"),
                                    "decision": e.get("decision"), "ok": e.get("ok"),
                                    "target_hash": str(e.get("target_hash") or "")[:16]})
        per_turn.append({"idx": idx, "completed": completed, "kind": kind,
                         "error": error, "answer": answer, "audit": audit_slice,
                         "calls": [], "spent_usd": round(max(0.0, (usage_after or 0) - (usage_before or 0)), 6)
                         if usage_before is not None else None})
    await engine.dispose()
    return {"turns": per_turn, "spent_usd": round(spent, 6)}


def _key_usage() -> float | None:
    try:
        from app.growth.keys import key_credit, load_eval_key
        k = load_eval_key()
        if not k:
            return None
        return float(key_credit(k).get("usage") or 0.0)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shadow-worker")
    ap.add_argument("--turns", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--tmp", required=True)
    a = ap.parse_args(argv)
    turns = json.loads(Path(a.turns).read_text())

    async def go():
        ok, detail, log = install_guard()
        if not ok:
            return {"ok": False, "error": detail}
        ok, detail2 = await self_test(log)
        if not ok:
            return {"ok": False, "error": detail2}
        rep = await run_turns(turns, a.budget, Path(a.tmp))
        # Attach the guard call log per turn is overkill; attach totals +
        # blocked names so the report states fidelity gaps exactly.
        rep["guard_calls"] = len(log.calls)
        rep["guard_blocked"] = log.blocked_names()
        rep["guard_previewed"] = sorted({c["name"] for c in log.calls if c["verdict"] == "previewed"})
        return {"ok": True, "report": rep}

    outcome = asyncio.run(go())
    if not outcome["ok"]:
        print(f"shadow worker refused: {outcome['error']}", file=sys.stderr)
        return 2
    Path(a.out).write_text(json.dumps(outcome["report"], indent=1))
    print(f"shadow worker: {len(turns)} turns, spent ${outcome['report'].get('spent_usd')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
