"""Turn the redacted failure queue into a repair brief.

`--apply` never merges or deploys. It stops at confirmation.
"""
from __future__ import annotations

from pathlib import Path

from app.fabric.failures import read_queue, redact


def build_brief(template: str, rows: list[dict]) -> str:
    """Copy the standing rules, then the open failures. Secrets stay redacted."""
    open_rows = [row for row in rows if str(row.get("status") or "open") == "open"]
    lines = [template.rstrip(), "", "## This run", ""]
    if not open_rows:
        lines.append("No open failures in the queue.")
        lines.append("")
        lines.append("Apply stops at CONFIRM. Nothing is merged or deployed.")
        return "\n".join(lines) + "\n"
    lines.append(f"Open failures: {len(open_rows)}.")
    lines.append("")
    for row in open_rows[:40]:
        utterance = redact(str(row.get("utterance") or "")).strip() or "(empty utterance)"
        error = redact(str(row.get("error") or "")).strip()
        lines.append(f"- {utterance}")
        if error:
            lines.append(f"  error: {error}")
    lines.append("")
    lines.append("Apply stops at CONFIRM. Nothing is merged or deployed.")
    return "\n".join(lines) + "\n"


def write_brief(output: str, *, apply: bool = False) -> int:
    """Write the brief from the live queue. Apply does not change the tree."""
    template_path = Path(__file__).resolve().parents[3] / "docs" / "templates" / "IMPROVE_BRIEF.md"
    template = template_path.read_text(encoding="utf-8") if template_path.is_file() else ""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    brief = build_brief(template, read_queue())
    destination.write_text(brief, encoding="utf-8")
    if destination.read_text(encoding="utf-8") != brief:
        print(f"brief at {destination} did not read back")
        return 1
    print(f"wrote {destination}")
    if apply:
        print("stopped at CONFIRM; nothing was merged or deployed")
    return 0
