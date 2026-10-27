"""GE6 briefs: self-contained repair briefs from all three sources.

Sources: the growth failure queue (top clusters), the fabric file queue
(open rows), LIFE suite failures (latest stored result), and the P2 queue
(open items, or one named --item). The brief embeds the standing rules,
the budget, and the instruction to add regression tests.
"""

from __future__ import annotations

STANDING_RULES = """Standing rules (binding on this run):
- Work ONLY inside this worktree and branch. Never touch another checkout,
  never merge, deploy, push, or edit the live tree.
- Trust nothing unverified: reproduce the failure first with a real command.
- Never weaken a test, oracle, budget, or threshold to make red green.
- Add a regression test for every fix; run the affected checks.
- No new dependencies. Lazy imports for heavy modules. No secrets in code,
  logs, or commits. Redact.transcript content stays in the worktree.
- Python: ruff-clean and mypy-clean on changed lines (repo config).
- Commit at the end with a clear message. Do not push.
""".strip()


def build(item_id: int | None = None, budget: float = 1.0) -> tuple[str, dict]:
    """Build the brief markdown + machine context. Returns (markdown, ctx)."""
    from ..learn import cluster as CL
    from ..learn import queue as Q
    from ..store import recent
    from . import p2 as P2

    P2.seed()
    ctx: dict = {"p2": [], "failures": [], "life_failures": []}
    lines = ["# Improve brief (growth engine)", ""]
    if item_id is not None:
        item = P2.get(item_id)
        if item is None:
            raise ValueError(f"no P2 item {item_id}")
        ctx["p2"] = [item]
        lines += [f"## Target P2 item #{item['id']}: {item['title']}", "",
                  item.get("detail") or "(no detail)", ""]
    else:
        items = P2.open_items()[:10]
        ctx["p2"] = items
        lines += ["## Open P2 items (pick the top unblocked one)", ""]
        for it in items:
            lines += [f"- #{it['id']}: {it['title']}"]
        lines += [""]
    fails = Q.list_recent(200)
    groups = CL.rank(CL.cluster(fails))[:8]
    ctx["failures"] = [{"key": k, "n": len(m),
                        "sample": (m[0].get("utterance") or m[0].get("receipts") or "")[:200]}
                       for k, m in groups]
    lines += ["## Top failure clusters (real life)", ""]
    for g in ctx["failures"]:
        lines += [f"- {g['key']} x{g['n']}: {g['sample']}"]
    lines += [""]
    try:
        from app.fabric.failures import read_queue
        frows = [r for r in read_queue() if str(r.get("status") or "open") == "open"][:10]
    except Exception:
        frows = []
    if frows:
        lines += ["## Fabric file queue (open)", ""]
        for r in frows:
            lines += [f"- {(r.get('utterance') or '')[:150]}"]
        lines += [""]
        ctx["fabric_queue"] = len(frows)
    for r in recent("life", 1):
        for f in (r.get("failures") or [])[:10]:
            ctx["life_failures"].append({"id": f.get("id"), "detail": f.get("detail")})
    if ctx["life_failures"]:
        lines += ["## LIFE suite failures (regressions of shipped behavior)", ""]
        for f in ctx["life_failures"]:
            lines += [f"- {f['id']}: {f['detail']}"]
        lines += [""]
    lines += ["## Budget",
              f"Eval spend cap for any suite runs this task triggers: ${budget:.2f} "
              f"(pass --budget to `ev growth run`; the runner clamps to room).",
              "Your own inference spends the owner's coding subscription; work efficiently.", "",
              "## " + STANDING_RULES.replace("\n", "\n"), ""]
    return "\n".join(lines) + "\n", ctx
