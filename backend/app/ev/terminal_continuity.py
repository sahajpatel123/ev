"""Terminal continuity: screenshot of terminal sessions -> resumed Ghostty.

Shared-agent note: pure-local skill (OCR + Ghostty driver, no model
calls). Safe to extend with new resume patterns; keep extract_* pure and
keep the honest receipt shapes — task_agent branches on them.


The owner shows Evie a screenshot of terminal sessions (opencode, Muse
Code CLI, plain shells) and asks to resume them. Everything here runs on
the local Mac with no cloud brain: find the image, OCR it with the local
vision provider, extract resume commands, open Ghostty windows, type the
commands, and verify by reading the screens back.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# opencode / Muse Code CLI resume shapes, line-local (never span newlines).
_RESUME_PATTERNS = (
    re.compile(r"(opencode[^\S\n]+(?:run[^\S\n]+)?[^\s]+(?:[^\S\n]+[^\s]+){0,3})", re.I),
    re.compile(
        r"(Muse[^\S\n]+(?:--continue|--resume(?:[^\S\n]+[^\s]+)?|[^\s]+)"
        r"(?:[^\S\n]+[^\s]+){0,3})",
        re.I,
    ),
    re.compile(
        r"(codex[^\S\n]+(?:resume[^\S\n]+[^\s]+|[^\s]+)(?:[^\S\n]+[^\s]+){0,3})",
        re.I,
    ),
)

_PROMPT_STRIP = re.compile(r"^(?:[~$\w\-./:]+[#$%>»❯]+\s*)+")


def extract_resume_commands(text: str, *, limit: int = 8) -> list[str]:
    """Pull candidate resume commands out of OCR text. Pure and total."""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _RESUME_PATTERNS:
        for match in pattern.finditer(text or ""):
            cleaned = _PROMPT_STRIP.sub("", match.group(1)).strip().rstrip("\\")
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if len(cleaned) < 3 or len(cleaned) > 300:
                continue
            key = cleaned.lower()
            if key not in seen:
                seen.add(key)
                found.append(cleaned)
            if len(found) >= limit:
                return found
    return found


def looks_like_terminal_resume(text: str) -> bool:
    """Classifier for the resume-a-terminal-session ask."""
    raw = (text or "").lower()
    if not raw:
        return False
    terminal = bool(
        re.search(r"\b(?:ghostty|terminal|iterm|tmux|opencode|claude\s+code|muse|cli)\b", raw)
    )
    resume = bool(
        re.search(r"\b(?:resume|reopen|reconnect|continue|restore|reattach|relaunch)\b", raw)
    )
    screenshot = "screenshot" in raw or "screen shot" in raw
    return terminal and (resume or screenshot)


def find_terminal_screenshots(folder: str | None = None, *, limit: int = 5) -> list[str]:
    """Newest PNG screenshots under the folder (default: ~/Desktop)."""
    root = Path(folder).expanduser() if folder else Path.home() / "Desktop"
    try:
        shots = sorted(
            (p for p in root.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    return [str(p) for p in shots[:limit]]


async def _ocr_text(image_path: str) -> tuple[str, dict[str, Any]]:
    from app.fabric.screen_pipeline import ocr_path

    result = await ocr_path(image_path)
    evidence = result.get("evidence") or {}
    return str(evidence.get("text") or ""), result


async def resume_from_screenshot(
    image_path: str,
    *,
    max_windows: int = 4,
    cwd: str = "",
) -> dict[str, Any]:
    """OCR one screenshot, open Ghostty windows, type resume commands.

    Returns an honest receipt: per-command launched/typed/verified flags.
    Nothing here touches the network; failures name the failed leg.
    """
    from app.fabric.ghostty import GhosttyScripted, probe

    probed = probe()
    evidence = probed.get("evidence") or {}
    if not probed.get("ok") or not evidence.get("bundle_present"):
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "ghostty_missing",
            "spoken": "Ghostty isn't installed, so I can't resume those sessions.",
        }
    text, ocr = await _ocr_text(image_path)
    if not ocr.get("ok") or not text.strip():
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": str(ocr.get("error") or "ocr_empty"),
            "spoken": "I couldn't read any text in that screenshot.",
        }
    commands = extract_resume_commands(text)
    if not commands:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "no_resume_commands",
            "spoken": "I read the screenshot but found no resume commands in it.",
            "ocr_chars": len(text),
        }
    driver = GhosttyScripted()
    launched: list[dict[str, Any]] = []
    for command in commands[:max_windows]:
        opened = driver.open_window(command="", cwd=cwd)
        if not opened.get("ok"):
            launched.append({"command": command, "launched": False, "error": opened.get("error")})
            continue
        wid = str((opened.get("evidence") or {}).get("window_id") or "")
        typed = driver.input_text(wid, command) if wid else {"ok": False, "error": "no_window_id"}
        read = driver.read_screen(wid) if typed.get("ok") else {"ok": False}
        launched.append(
            {
                "command": command,
                "launched": True,
                "window_id": wid,
                "typed": bool(typed.get("ok")),
                "verified": bool(read.get("ok")),
                "error": typed.get("error") if not typed.get("ok") else None,
            }
        )
    ok = bool(launched) and all(item.get("launched") and item.get("typed") for item in launched)
    n = sum(1 for item in launched if item.get("launched") and item.get("typed"))
    return {
        "ok": ok,
        "executed": any(item.get("launched") for item in launched),
        "verified": all(item.get("verified") for item in launched if item.get("launched")),
        "error": None if ok else "partial_resume",
        "spoken": (
            f"Resumed {n} of {len(launched)} terminal sessions in Ghostty."
            if ok or n
            else "I couldn't resume those sessions in Ghostty."
        ),
        "sessions": launched,
        "image": image_path,
    }


async def resume_named_sessions(
    commands: list[str], *, cwd: str = ""
) -> dict[str, Any]:
    """Type explicit commands into fresh Ghostty windows. No OCR leg."""
    from app.fabric.ghostty import GhosttyScripted, probe

    probed = probe()
    if not probed.get("ok") or not (probed.get("evidence") or {}).get("bundle_present"):
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "ghostty_missing",
            "spoken": "Ghostty isn't installed, so I can't resume those sessions.",
        }
    if not commands:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "no_commands",
            "spoken": "Tell me which commands to resume.",
        }
    driver = GhosttyScripted()
    launched: list[dict[str, Any]] = []
    for command in commands[:8]:
        opened = driver.open_window(command="", cwd=cwd)
        if not opened.get("ok"):
            launched.append({"command": command, "launched": False, "error": opened.get("error")})
            continue
        wid = str((opened.get("evidence") or {}).get("window_id") or "")
        typed = driver.input_text(wid, command) if wid else {"ok": False, "error": "no_window_id"}
        launched.append(
            {
                "command": command,
                "launched": True,
                "window_id": wid,
                "typed": bool(typed.get("ok")),
                "error": typed.get("error") if not typed.get("ok") else None,
            }
        )
    ok = bool(launched) and all(item.get("launched") and item.get("typed") for item in launched)
    n = sum(1 for item in launched if item.get("launched") and item.get("typed"))
    return {
        "ok": ok,
        "executed": any(item.get("launched") for item in launched),
        "verified": ok,
        "error": None if ok else "partial_resume",
        "spoken": (
            f"Resumed {n} of {len(launched)} terminal sessions in Ghostty."
            if ok or n
            else "I couldn't resume those sessions in Ghostty."
        ),
        "sessions": launched,
    }
