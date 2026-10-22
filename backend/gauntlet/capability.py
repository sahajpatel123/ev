"""Capability gauntlet: can Evie actually *do* the work?

The behavioural gauntlet in :mod:`gauntlet.runner` proves the fabric lanes
behave when a scripted planner has already chosen the tool call. That is the
plumbing pass rate. It is not evidence that Evie can figure out what to do.

This module answers the other question. Each case is:

- a **sandboxed world** — a temporary HOME with its own Desktop, Documents,
  Downloads, Code and Projects, so file and shell effects land somewhere
  disposable instead of on the owner's Mac;
- a **real brain** — the production agent loop, with the unified tool
  surface (the semantic capability graph plus the hands), talking to the
  live provider;
- **world-state oracles** — what must be true of the sandbox afterwards, and
  what the run must have said.

The provider is real by default and injectable, so the harness itself is
testable without a key.

Two things are deliberately *not* sandboxed: the database, and the owner's
real personal data. A capability gauntlet that answers "can Evie read mail"
against an empty database proves nothing, so reads go to the real store.
Writes are the risk, and they are contained three ways: the sandbox HOME,
the owner-autonomy setting that decides whether a mutating step parks for
approval, and a memory adapter that swallows anything the run tries to send.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Sub-directories of the sandbox home, mirroring a real owner's layout.
SANDBOX_DIRS = ("Desktop", "Documents", "Downloads", "Code", "Projects")


@dataclass
class Case:
    """One capability case.

    ``goal`` is what the owner says. ``oracle`` is what must be true
    afterwards. The oracle is fixed before the run, exactly as in the
    behavioural gauntlet: the planner never sees it.
    """

    id: str
    tier: str
    title: str
    goal: str
    oracle: list[dict] = field(default_factory=list)
    setup: list[dict] = field(default_factory=list)
    max_tool_calls: int = 12
    max_seconds: int = 180
    approved: bool = True
    #: Capabilities that need the owner's live data. These are reported
    #: separately: they depend on what the owner actually has stored.
    live_data: bool = False
    notes: str = ""


@dataclass
class Sandbox:
    """A disposable HOME for one case."""

    root: Path | None = None
    home: Path | None = None
    _saved_home: str | None = None
    _saved_root: str | None = None
    _saved_allowed: str | None = None

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = Path(tempfile.mkdtemp(prefix="ev-cap-"))
        if self.home is None:
            self.home = self.root / "home"
            self.home.mkdir(parents=True, exist_ok=True)
            for name in SANDBOX_DIRS:
                (self.home / name).mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(cls) -> Sandbox:
        return cls()

    def __enter__(self) -> Sandbox:
        self._saved_home = os.environ.get("HOME")
        self._saved_root = os.environ.get("EV_FABRIC_ROOTS")
        os.environ["HOME"] = str(self.home)
        # The fabric lanes resolve their roots from HOME; this extra root
        # makes the sandbox Code folder reachable through the lane router.
        os.environ["EV_FABRIC_ROOTS"] = str(self.home / "Code")
        # The boundary that makes "approved" mean "approved inside the
        # sandbox". Without it an approved step may write any absolute path
        # on the owner's real Mac — correct in production, catastrophic in a
        # test.
        os.environ["EV_AGENT_ALLOWED_ROOTS"] = str(self.home)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home
        if self._saved_root is None:
            os.environ.pop("EV_FABRIC_ROOTS", None)
        else:
            os.environ["EV_FABRIC_ROOTS"] = self._saved_root
        if self._saved_allowed is None:
            os.environ.pop("EV_AGENT_ALLOWED_ROOTS", None)
        else:
            os.environ["EV_AGENT_ALLOWED_ROOTS"] = self._saved_allowed
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read(self, rel: str) -> str:
        return (self.home / rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return (self.home / rel).exists()


def _apply_setup(sandbox: Sandbox, setup: list[dict]) -> None:
    for item in setup or []:
        if item.get("path"):
            sandbox.write(item["path"], item.get("text") or "")
        if item.get("dir"):
            (sandbox.home / item["dir"]).mkdir(parents=True, exist_ok=True)
        if item.get("dir"):
            (sandbox.home / item["dir"]).mkdir(parents=True, exist_ok=True)
        if item.get("git"):
            import subprocess

            repo = sandbox.home / item["dir"]
            repo.mkdir(parents=True, exist_ok=True)
            env = {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                "HOME": str(sandbox.home),
            }
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=False, timeout=20, env=env)
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False, timeout=20, env=env)
            subprocess.run(
                ["git", "-c", "user.email=gauntlet@example.com", "-c", "user.name=Gauntlet",
                 "-c", "commit.gpgsign=false", "commit", "-m", "initial"],
                cwd=repo, capture_output=True, check=False, timeout=20, env=env,
            )


def _step_text(steps: list[Any]) -> str:
    """Every byte the run produced, for substring oracles."""
    parts: list[str] = []
    for step in steps:
        for attr in ("tool", "error"):
            value = getattr(step, attr, None)
            if value:
                parts.append(str(value))
        result = getattr(step, "result", None)
        if result is not None:
            parts.append(str(result))
    return " ".join(parts)


def _check(sandbox: Sandbox, oracle: list[dict], steps: list[Any], summary: str,
           asked: bool, planned: bool) -> list[str]:
    """World-state oracles. Each returns the problems it found."""
    problems: list[str] = []
    blob = _step_text(steps)
    tools = [str(getattr(s, "tool", "") or "") for s in steps]

    for item in oracle or []:
        kind = item.get("kind")
        if kind == "file_exists":
            if not sandbox.exists(item["path"]):
                problems.append(f"file_exists {item['path']}")
        elif kind == "file_absent":
            if sandbox.exists(item["path"]):
                problems.append(f"file_absent {item['path']}")
        elif kind == "file_contains":
            if not sandbox.exists(item["path"]):
                problems.append(f"file_contains {item['path']} (missing)")
            elif item["text"] not in sandbox.read(item["path"]):
                problems.append(f"file_contains {item['path']}")
        elif kind == "file_not_contains":
            if sandbox.exists(item["path"]) and item["text"] in sandbox.read(item["path"]):
                problems.append(f"file_not_contains {item['path']}")
        elif kind == "file_changed":
            path = sandbox.home / item["path"]
            if not path.exists():
                problems.append(f"file_changed {item['path']} (missing)")
            elif path.read_text(encoding="utf-8") == item.get("before"):
                problems.append(f"file_changed {item['path']} (unchanged)")
        elif kind == "stdout_contains":
            if item["text"] not in blob:
                problems.append("stdout_contains")
        elif kind == "answer_contains":
            if item["text"].lower() not in (summary or "").lower():
                problems.append(f"answer_contains {item['text']!r}")
        elif kind == "answer_not_contains":
            if item["text"].lower() in (summary or "").lower():
                problems.append(f"answer_not_contains {item['text']!r}")
        elif kind == "used_tool":
            if item["name"] not in tools:
                problems.append(f"used_tool {item['name']}")
        elif kind == "avoided_tool":
            if item["name"] in tools:
                problems.append(f"avoided_tool {item['name']}")
        elif kind == "avoided_send":
            # A send can arrive under several capability names. What matters
            # is that nothing went out, not which tool carried it — but a
            # *read* through the same tool is not a send.
            sent: list[str] = []
            for step in steps:
                name = str(getattr(step, "tool", "") or "")
                args = getattr(step, "arguments", None) or {}
                if name in {"life.send", "phone.call"}:
                    sent.append(name)
                elif name == "comms.act":
                    if str(args.get("op") or "").lower() in {"send", "reply", "forward"}:
                        sent.append(f"comms.act:{args.get('op')}")
                elif name == "digital.act" and str(args.get("operation") or "").lower() in {
                    "send", "reply", "forward",
                }:
                    sent.append(f"digital.act:{args.get('operation')}")
            if sent:
                problems.append(f"avoided_send ({sent})")
        elif kind == "used_any_tool":
            if not any(name in tools for name in item["names"]):
                problems.append(f"used_any_tool {item['names']}")
        elif kind == "asked":
            if not asked:
                problems.append("asked")
        elif kind == "not_asked":
            if asked:
                problems.append("not_asked")
        elif kind == "planned":
            if not planned:
                problems.append("planned")
        elif kind == "refused":
            # A refusal: no effect was produced and the run did not claim
            # success. This is the honest-failure oracle.
            ok_steps = [s for s in steps if getattr(s, "ok", False)]
            if ok_steps and any(
                getattr(s, "tool", "") not in {"finish", "ask_owner", "todo_write"}
                for s in ok_steps
            ):
                problems.append("refused (an effect succeeded)")
            if _claims_success(summary):
                problems.append("refused (claimed success)")
        elif kind == "all_steps_ok":
            broken = [
                str(getattr(s, "tool", "") or "")
                for s in steps
                if not getattr(s, "ok", False)
            ]
            if broken:
                problems.append(f"all_steps_ok (failed: {broken})")
        elif kind == "max_tool_calls" and len(steps) > int(item["n"]):
            problems.append(f"max_tool_calls {len(steps)} > {item['n']}")
    return problems


def _claims_success(summary: str) -> bool:
    """Did the run claim an effect it cannot show?"""
    from app.fabric.honesty import claims_success

    try:
        return bool(claims_success(summary or ""))
    except Exception:  # pragma: no cover - honesty helper must not break a run
        return False


def _real_home_snapshot(home: Path) -> dict[str, tuple[int, float]]:
    """A fingerprint of the owner's real home, for the containment guard.

    The sandbox redirects HOME, but a lane that resolves an absolute path, or
    a shell that resets HOME, can still reach the real Mac. A capability
    gauntlet that can touch the owner's Desktop is not a test, it is an
    incident — so the real home is fingerprinted before and after every case
    and any change fails the case loudly.

    The real home is captured *before* the sandbox is entered, because
    inside the sandbox ``Path.home()`` is the sandbox.
    """
    out: dict[str, tuple[int, float]] = {}
    for name in ("Desktop", "Documents", "Downloads"):
        base = home / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            out[str(path)] = (stat.st_size, stat.st_mtime)
    return out


async def run_case(case: Case, *, provider: Any = None) -> dict:
    """Run one case in a sandbox with the real agent loop."""
    from app.agent.loop import AgentLimits, run_agent

    real_home = Path.home().resolve()
    real_before = _real_home_snapshot(real_home)
    # The sandbox must be *entered*, not merely created: entering is what
    # redirects HOME, and HOME is what the lanes resolve against.
    with Sandbox() as sandbox:
        _apply_setup(sandbox, case.setup)

        # Anything the run tries to send goes to a list, not to a device.
        try:
            from app.fabric.comms import MemoryAdapter, set_adapter

            set_adapter(MemoryAdapter())
        except Exception:  # pragma: no cover - import guard
            pass

        brain = None
        if provider is None:
            from app.agent.brain import build_agent_provider, resolve_agent_brain

            brain = resolve_agent_brain()
            if not brain.available:
                return {
                    "id": case.id, "tier": case.tier, "title": case.title,
                    "passed": None, "skipped": True,
                    "detail": f"no brain available: {brain.reason}",
                    "problems": [], "steps": [], "tool_calls": 0,
                    "elapsed_ms": 0, "answer": "", "live_data": case.live_data,
                }
            provider = build_agent_provider(brain)

        started = time.perf_counter()
        result = await run_agent(
            case.goal,
            root=sandbox.home,
            provider=provider,
            approved=case.approved,
            limits=AgentLimits(
                max_steps=max(2, case.max_tool_calls + 2),
                max_tool_calls=case.max_tool_calls,
                max_seconds=case.max_seconds,
            ),
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        steps = list(result.steps)
        # The loop intercepts ask_owner and finish, so neither appears as a
        # step. A run that ended in needs_input asked the owner, whether or
        # not the step was recorded.
        asked = bool(result.needs_input) or any(str(s.tool) == "ask_owner" for s in steps)
        planned = any(str(s.tool) == "todo_write" for s in steps)
        summary = result.summary or ""
        problems = _check(sandbox, case.oracle, steps, summary, asked, planned)

        # A run that ended in an outright failure is a failure even if the
        # oracles happened to pass.
        if result.status == "failed":
            problems.append(f"run failed: {result.error}")
        if result.needs_input and not asked:
            problems.append("needs_input without ask_owner")

        # Containment: the owner's real home must be exactly as it was.
        real_after = _real_home_snapshot(real_home)
        touched = sorted(
            set(real_after) - set(real_before)
            | {p for p in set(real_after) & set(real_before)
               if real_after[p] != real_before[p]}
        )
        if touched:
            problems.append(f"ESCAPED THE SANDBOX: {touched[:5]}")

        return {
            "id": case.id,
            "tier": case.tier,
            "title": case.title,
            "passed": not problems,
            "skipped": False,
            "problems": problems,
            "status": result.status,
            "tools": [str(s.tool) for s in steps],
            "step_detail": [
                {
                    "tool": str(s.tool),
                    "policy": s.policy,
                    "ok": bool(s.ok),
                    "error": s.error,
                    "arguments": s.arguments,
                }
                for s in steps
            ],
            "tool_calls": result.tool_calls,
            "elapsed_ms": elapsed_ms,
            "answer": summary,
            "live_data": case.live_data,
            "notes": case.notes,
        }


def summarize(rows: list[dict]) -> dict:
    """Pass rates per tier, and the headline numbers that matter."""
    def rate(subset: list[dict]) -> float:
        if not subset:
            return 0.0
        return round(100.0 * sum(1 for r in subset if r.get("passed")) / len(subset), 2)

    ran = [r for r in rows if not r.get("skipped")]
    by_tier: dict[str, dict] = {}
    for tier in sorted({r["tier"] for r in rows}):
        subset = [r for r in ran if r["tier"] == tier]
        by_tier[tier] = {
            "n": len(subset),
            "pass_rate": rate(subset),
            "mean_tool_calls": round(
                sum(r.get("tool_calls") or 0 for r in subset) / len(subset), 1
            ) if subset else 0.0,
            "mean_elapsed_ms": int(
                sum(r.get("elapsed_ms") or 0 for r in subset) / len(subset)
            ) if subset else 0,
        }
    return {
        "n": len(rows),
        "ran": len(ran),
        "skipped": len(rows) - len(ran),
        "pass_rate": rate(ran),
        "by_tier": by_tier,
        "failed": [
            {"id": r["id"], "tier": r["tier"], "problems": r.get("problems", [])}
            for r in ran if not r.get("passed")
        ],
    }
