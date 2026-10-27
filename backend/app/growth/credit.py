"""Credit + cap watch: warn before eval (or live) spend runs out.

`ev growth credit`: prints numbers only, appends docs/OWNER_ACTIONS_EVAL.md on
warning, and best-effort pushes a HUD/phone notice through the live notify
channel. Exit 2 when the owner must act, 0 otherwise.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

WARN_MIN_USD = 2.0
WARN_FRAC = 0.20


def _owner_actions_path() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "OWNER_ACTIONS_EVAL.md"


def check() -> dict:
    from .budget import caps, spent
    from .keys import key_credit, load_eval_key
    from .result import utcnow

    out: dict = {"now": utcnow(), "eval": None, "caps": {}, "warnings": []}
    now = out["now"]
    daily, monthly = caps()
    spent_day, spent_month = spent(now[:10], now[:7])
    out["caps"] = {"daily": daily, "monthly": monthly,
                   "spent_day": round(spent_day, 4), "spent_month": round(spent_month, 4)}
    ev = load_eval_key()
    if not ev:
        out["warnings"].append("no eval key set: run `ev keys set openrouter-eval` (suites cannot spend)")
        return out
    try:
        c = key_credit(ev)
    except Exception as e:  # noqa: BLE001 - provider errors are data
        out["warnings"].append(f"eval credit check failed: {type(e).__name__}")
        return out
    out["eval"] = {k: c.get(k) for k in ("label", "usage", "limit", "remaining", "is_free_tier")}
    rem = c.get("remaining")
    base = c.get("limit") if c.get("limit") else monthly
    if rem is not None and (rem < WARN_MIN_USD or (base and rem < WARN_FRAC * base)):
        out["warnings"].append(
            f"eval key low: ${rem:.2f} remaining (warn under ${WARN_MIN_USD} or 20%). "
            "Top up the eval key; suites will refuse when caps hit.")
    return out


def _write_owner_file(warnings: list[str]) -> Path:
    p = _owner_actions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# OWNER-ACTION: eval budget (growth engine)", ""]
    for w in warnings:
        lines.append(f"- [ ] {w}")
    lines += ["", "Suggested standing setup: eval key ~$20/month cap, live key ~$10/month cap",
              "(caps live on the OpenRouter dashboard + EV_GROWTH_EVAL_*_CAP_USD).", ""]
    p.write_text("\n".join(lines))
    return p


def _push_notice(text: str) -> bool:
    import warnings

    try:
        from clients.cli import notify_send

        async def _go():
            async with asyncio.timeout(15.0):
                await notify_send("Evie eval budget", text, priority=0.8,
                                  tier="important", kind="growth", source="growth-credit")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            asyncio.run(_go())
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    del argv
    from .keys import load_eval_key

    rep = check()
    print(f"eval key: {'SET' if load_eval_key() else 'UNSET'}")
    if rep["eval"]:
        e = rep["eval"]
        print(f"  usage=${e['usage']:.2f} limit={e['limit']} remaining={e['remaining']}")
    c = rep["caps"]
    print(f"  caps: day ${c['spent_day']:.2f}/${c['daily']:.2f} month ${c['spent_month']:.2f}/${c['monthly']:.2f}")
    if not rep["warnings"]:
        print("ok: no budget action needed.")
        return 0
    for w in rep["warnings"]:
        print(f"WARN: {w}")
    p = _write_owner_file(rep["warnings"])
    print(f"wrote {p}")
    pushed = _push_notice("; ".join(rep["warnings"]))
    print(f"HUD/phone notice: {'sent' if pushed else 'unavailable (server/auth unreachable)'}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
