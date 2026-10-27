"""GE6 coding-CLI detection. Every flag below was read from that CLI's
own --help on this machine (never assumed). Only muse exec is PROVEN
headless here (META-OK smoke); others are detected and attempted with
clear errors.
"""

from __future__ import annotations

import shutil
import subprocess

# name -> (probe argv, headless argv prefix, notes)
TABLE = {
    "muse": (["muse", "exec", "--provider", "echo", "probe"],
             ["muse", "exec", "--disable-approval"],
             "sandbox stays ON (default); wall timeout bounds the run"),
    "claude": (["claude", "-p", "probe"],
               ["claude", "-p", "--dangerously-skip-permissions"],
               "UNPROVEN here; needs authed claude"),
    "codex": (["codex", "exec", "probe"],
              ["codex", "exec", "--approve-for-me"],
              "UNPROVEN here; workspace-write sandbox"),
}


def detect() -> list[str]:
    """Installed CLIs in preference order."""
    return [name for name in ("muse", "claude", "codex") if shutil.which(name)]


def probe(name: str, timeout: int = 120) -> tuple[bool, str]:
    """Harmless headless smoke for one CLI. Returns (ok, detail)."""
    if name not in TABLE or not shutil.which(name):
        return False, f"{name} not installed"
    try:
        p = subprocess.run(TABLE[name][0], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{name} probe timed out after {timeout}s"
    ok = p.returncode == 0
    return ok, ((p.stdout or "") + (p.stderr or ""))[-300:] if not ok else "probe ok"


def headless_cmd(name: str, prompt: str) -> list[str]:
    """Headless invocation for prompt text (verified flags only)."""
    if name == "muse":
        raise ValueError("muse takes --prompt-file; use headless_cmd_file")
    if name == "claude":
        return [*TABLE["claude"][1], prompt]
    if name == "codex":
        return [*TABLE["codex"][1], prompt]
    raise ValueError(f"unknown coding CLI: {name}")


def headless_cmd_file(name: str, prompt_file: str) -> list[str]:
    """Headless invocation for a prompt file (muse only, verified)."""
    if name != "muse":
        raise ValueError(f"{name} has no verified prompt-file flag; pass text")
    return [*TABLE["muse"][1], "--prompt-file", prompt_file]
