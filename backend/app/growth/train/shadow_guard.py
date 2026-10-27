"""Shadow-replay guard: effects are impossible by construction, not by hope.

The loop's own ``prepare_only`` mode covers most mutating tools (verified
per-tool, see the audit in docs/GROWTH_ENGINE_LOG.md), but these holes
remain and are closed HERE, at the shadow layer:

- ``agent.run`` never propagates prepare_only: subagents would execute.
- ``computer.observe`` / ``look.capture`` capture live owner pixels (privacy:
  screen/cam up to a remote brain is outside the replay's mandate).
- Fabric ``skills_act`` is not in the prepare_only thread-through set and is
  not in MUTATING: real HANDS effects would execute.
- Fabric ``screen_ref`` capture ops read live pixels (same privacy reason).
- ``digital.act`` mutating non-send ops are service-specific and unaudited.

The guard wraps ``execute_semantic`` and ``run_fabric_tool`` inside the
shadow worker process only. Every blocked call is recorded (name + op) so
the comparison can state its fidelity gaps exactly. Reads pass through;
writes to the passed (scratch) DB session are contained by construction.
"""

from __future__ import annotations

from typing import Any

# Semantic tool names never executed in shadow, regardless of prepare_only.
SHADOW_BLOCKED_SEMANTIC = frozenset({
    "agent.run",          # subagents do not inherit prepare_only (verified)
    "computer.observe",   # live pixels -> remote brain (privacy)
    "look.capture",       # camera/screen capture (privacy)
})

# Fabric lane names never executed in shadow.
SHADOW_BLOCKED_FABRIC = frozenset({
    "skills_act",         # HANDS effects, outside prepare_only threading
})

# Fabric screen ops blocked (capture reads live pixels); pure queries pass.
SHADOW_BLOCKED_SCREEN_OPS = frozenset({"capture", "snap", "record"})

# digital.act ops allowed: conservative read set. Everything else blocks.
DIGITAL_READ_OPS = frozenset({
    "list", "status", "read", "get", "search", "lookup", "show", "fetch",
})


def shadow_failure(name: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "verified": False, "shadow_blocked": True,
            "error": "SHADOW_BLOCKED",
            "spoken": "Shadow replay: I will not run that effect.",
            "diagnosis": "SHADOW_BLOCKED",
            "evidence": {"tool": name, "reason": reason}}


class ShadowLog:
    """Exact record of what the guard allowed, blocked, and previewed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, layer: str, name: str, verdict: str, detail: str = "") -> None:
        self.calls.append({"layer": layer, "name": name, "verdict": verdict,
                           "detail": detail[:200]})

    def blocked_names(self) -> list[str]:
        return sorted({c["name"] for c in self.calls if c["verdict"] == "blocked"})


def wrap_execute_semantic(orig, log: ShadowLog):
    """Wrap app.cognitive.executor.execute_semantic with the shadow guard."""

    async def guarded(session, name: str, arguments: dict, **kw):
        if name in SHADOW_BLOCKED_SEMANTIC:
            log.record("semantic", name, "blocked", "shadow block list")
            return shadow_failure(name, "shadow block list")
        if name == "digital.act":
            op = str((arguments or {}).get("operation") or "").lower()
            if op and op not in DIGITAL_READ_OPS:
                log.record("semantic", name, "blocked", f"non-read digital op: {op}")
                return shadow_failure(name, f"non-read digital op: {op}")
        result = await orig(session, name, arguments, **kw)
        if isinstance(result, dict) and (result.get("prepare_only") or
                                         result.get("diagnosis") == "POLICY_BLOCKED"):
            log.record("semantic", name, "previewed",
                       str(result.get("diagnosis") or "prepare_only"))
        else:
            log.record("semantic", name, "allowed")
        return result

    return guarded


def wrap_fabric_tool(orig, log: ShadowLog):
    """Wrap app.fabric.dispatch_tools.run_fabric_tool with the shadow guard."""

    async def guarded(session, name: str, args: dict, **kw):
        if name in SHADOW_BLOCKED_FABRIC:
            log.record("fabric", name, "blocked", "shadow block list")
            return shadow_failure(name, "shadow block list")
        if name == "screen_ref":
            op = str((args or {}).get("op") or "").lower()
            if op in SHADOW_BLOCKED_SCREEN_OPS or "captur" in op or "snap" in op:
                log.record("fabric", name, "blocked", f"capture op: {op}")
                return shadow_failure(name, f"capture op: {op}")
        result = await orig(session, name, args, **kw)
        if isinstance(result, dict) and result.get("prepare_only"):
            log.record("fabric", name, "previewed", "prepare_only receipt")
        else:
            log.record("fabric", name, "allowed")
        return result

    return guarded
