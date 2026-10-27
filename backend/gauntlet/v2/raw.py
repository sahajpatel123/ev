"""RAW-HIGH / RAW-MEDIUM: the scorecard's minimal-harness baseline.

Same brain model as EVIE (the comparison isolates the harness, not the
model), stateless per turn: each turn sees only a short system prompt plus
the current utterance. No doctrine, no memory, no policy, no confirmations,
no follow-up questions answered. Tools act directly on the world home and
the fixture server, so the same oracles grade both configs; tool-identity
oracles are skipped for RAW (see ``runner.TOOL_IDENTITY_KINDS``).

Spends real brain calls: every run goes through the eval key once
``ev keys set openrouter-eval`` exists (world env maps it in); until then
RAW runs are blocked, never silently charged to the live key.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _world_root() -> Path:
    return Path(os.environ["GAUNTLET_WORLD_ROOT"])


def _effort() -> str:
    return os.environ.get("GAUNTLET_RAW_EFFORT", "high")

SYSTEM = (
    "You are a helpful assistant completing one task. "
    "Files live under {home}; use paths relative to it. "
    "Use fixture_act for mail, messages, contacts and calendar. "
    "Complete the task with the tools, then reply briefly."
)


def _scrubbed_env(home: Path) -> dict[str, str]:
    """Child env with no secrets: the model must never see keys via `env`."""
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": "C",
        "TZ": "UTC",
    }


class RawTools:
    """Minimal world tools. Every failure is a value, never an exception."""

    def __init__(self, home: Path, fixture_url: str) -> None:
        self.home = home
        self.fixture_url = fixture_url.rstrip("/")

    # ------------------------------------------------------------ confinement

    def _resolve(self, path: str) -> Path | None:
        candidate = (self.home / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        try:
            candidate.relative_to(self.home.resolve())
        except ValueError:
            return None
        return candidate

    # ------------------------------------------------------------------ tools

    def read_file(self, args: dict) -> dict:
        target = self._resolve(str(args.get("path") or ""))
        if target is None or not target.is_file():
            return {"ok": False, "error": "not_found"}
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "text": text[:20000], "truncated": len(text) > 20000}

    def write_file(self, args: dict) -> dict:
        target = self._resolve(str(args.get("path") or ""))
        if target is None:
            return {"ok": False, "error": "outside_world"}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(args.get("content") or ""), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target.relative_to(self.home))}

    def list_dir(self, args: dict) -> dict:
        target = self._resolve(str(args.get("path") or "."))
        if target is None or not target.is_dir():
            return {"ok": False, "error": "not_found"}
        try:
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "entries": names[:200]}

    def run_shell(self, args: dict) -> dict:
        cmd = str(args.get("command") or "")
        if not cmd.strip():
            return {"ok": False, "error": "empty_command"}
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=min(float(args.get("timeout_s") or 30), 120),
                cwd=str(self.home), env=_scrubbed_env(self.home),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[:8000], "stderr": (proc.stderr or "")[:2000]}

    def http_get(self, args: dict) -> dict:
        url = str(args.get("url") or "")
        allowed = urllib.parse.urlparse(self.fixture_url)
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return {"ok": False, "error": "bad_url"}
        if parsed.netloc != allowed.netloc:
            return {"ok": False, "error": "host_not_allowed"}
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
                body = resp.read(20000)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}"}
        return {"ok": True, "status": 200, "body": body.decode("utf-8", "replace")}

    def fixture_act(self, args: dict) -> dict:
        action = str(args.get("action") or "").strip().strip("/")
        if not action or "/" in action or " " in action:
            return {"ok": False, "error": "bad_action"}
        data = json.dumps(args.get("args") or {}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            f"{self.fixture_url}/actions/{action}", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return {"ok": True, "result": json.loads(resp.read().decode("utf-8"))}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}"}

    # ---------------------------------------------------------------- dispatch

    def call(self, name: str, args: dict) -> dict:
        method = getattr(self, str(name).removeprefix("raw."), None)
        if not callable(method) or str(name) == "call":
            return {"ok": False, "error": f"unknown_tool {name}"}
        try:
            out = method(args if isinstance(args, dict) else {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return out if isinstance(out, dict) else {"ok": True, "result": out}


def tool_specs() -> list:
    from app.contracts import ToolSpec

    def spec(name: str, description: str, properties: dict, required: list[str]) -> ToolSpec:
        return ToolSpec(name=name, description=description, parameters={
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False})

    return [
        spec("raw.read_file", "Read a UTF-8 text file under the world home.",
             {"path": {"type": "string"}}, ["path"]),
        spec("raw.write_file", "Write a UTF-8 text file under the world home.",
             {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        spec("raw.list_dir", "List a directory under the world home.",
             {"path": {"type": "string"}}, []),
        spec("raw.run_shell", "Run one shell command with cwd at the world home.",
             {"command": {"type": "string"}, "timeout_s": {"type": "number"}},
             ["command"]),
        spec("raw.http_get", "GET one URL on the fixture server only.",
             {"url": {"type": "string"}}, ["url"]),
        spec("raw.fixture_act", "One fixture action (mail, messages, contacts, calendar).",
             {"action": {"type": "string"},
              "args": {"type": "object", "additionalProperties": True}}, ["action"]),
    ]


async def run_turn(say: str, tools: RawTools, provider, *, max_steps: int = 30,
                   time_budget_s: float = 240.0) -> dict[str, Any]:
    """One stateless raw turn. Returns reply text, trace rows and usage rows."""
    from app.contracts import ChatMessage

    deadline = time.monotonic() + time_budget_s
    messages = [ChatMessage(role="system", content=SYSTEM.format(home=tools.home)),
                ChatMessage(role="user", content=say)]
    trace: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    specs = tool_specs()
    text = ""
    for _ in range(max_steps):
        if time.monotonic() > deadline:
            break
        result = await provider.chat_with_tools(messages, specs)
        usage.append({"url": "openrouter", "model": result.model, "usage": result.usage or {}})
        text = result.text or ""
        calls = list(result.tool_calls or [])
        if not calls:
            break
        messages.append(ChatMessage(role="assistant", content=text, tool_calls=calls))
        for call in calls:
            started = time.time()
            out = tools.call(call.name, call.arguments)
            trace.append({"t": round(started, 3), "kind": "semantic",
                          "tool": call.name, "args": json.dumps(call.arguments, default=str)[:600],
                          "ok": bool(out.get("ok")), "result": json.dumps(out, default=str)[:800]})
            messages.append(ChatMessage(role="tool", content=json.dumps(out, default=str)[:4000],
                                        tool_call_id=call.id))
    return {"reply": text, "trace": trace, "usage": usage}


async def _run(case: dict[str, Any], home: Path, world_root: Path) -> dict[str, Any]:
    from gauntlet.v2.fixture_server import FixtureServer, WorldState

    state = WorldState(world_root / "state.json", case.get("world") or {})
    server = FixtureServer(state).start()
    from app.gateway.openrouter_muse import MuseSparkProvider

    provider = MuseSparkProvider()
    provider.reasoning_effort = _effort()
    tools = RawTools(home, server.base_url)
    replies: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    errors: list[str] = []
    limit = float(case.get("max_seconds") or 240)
    try:
        for turn in case.get("turns") or []:
            text = str(turn.get("say") or "").replace("{fixture}", server.base_url)
            started = time.perf_counter()
            try:
                out = await run_turn(text, tools, provider, time_budget_s=limit)
            except Exception as exc:  # noqa: BLE001 - a crashed turn is a result
                errors.append(f"turn crashed: {type(exc).__name__}: {exc}")
                out = {"reply": "", "trace": [], "usage": []}
            replies.append({"said": text, "reply": out["reply"], "channel": "raw",
                            "elapsed_ms": int((time.perf_counter() - started) * 1000)})
            trace.extend(out["trace"])
            usage.extend(out["usage"])
    finally:
        server.stop()
    return {"replies": replies, "confirmations": [], "tasks": [], "errors": errors,
            "trace": trace, "usage": usage, "elapsed_s": 0.0}


def main() -> int:
    from gauntlet.v2 import tripwire

    world_root = _world_root()
    tripwire.install(world_root, os.environ.get("GAUNTLET_REAL_HOME", ""))
    case_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    case = json.loads(case_path.read_text(encoding="utf-8"))
    home = world_root / "home"
    try:
        run = asyncio.run(_run(case, home, world_root))
    except Exception as exc:  # noqa: BLE001 - the runner records a crashed world
        run = {"replies": [], "confirmations": [], "tasks": [],
               "errors": [f"world crashed: {type(exc).__name__}: {exc}",
                          traceback.format_exc()[-3000:]],
               "trace": [], "usage": [], "elapsed_s": 0}
    run["tripwire"] = tripwire.read_log(world_root)
    out_path.write_text(json.dumps(run, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
