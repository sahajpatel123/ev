"""Run one turn-gauntlet case inside its world. Invoked as a subprocess.

    python -m gauntlet.v2.worker <case.json> <result.json>

The runner builds the world's environment (``world.World.env``) and starts
this module with it, so the app imports with world settings from the first
line. The case's turns go through ``POST /v1/chat`` over ASGI: the same
route the CLI and the PWA use, with auth, filter, kernel routing and the
agent task lane. A scripted owner answers confirmations exactly as the case
says (physically, by typing, by voice, or not at all).

The worker sees the whole case because it is the referee: it applies the
setup, plays the owner and grades the oracle. The brain never does. It gets
only what any owner turn would carry.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

WORLD_ROOT = Path(os.environ["GAUNTLET_WORLD_ROOT"])
REAL_HOME = os.environ.get("GAUNTLET_REAL_HOME", "")


def _install_tripwire() -> None:
    from gauntlet.v2 import tripwire

    tripwire.install(WORLD_ROOT, REAL_HOME)


_install_tripwire()

# ---------------------------------------------------------------- telemetry

TRACE: list[dict[str, Any]] = []
USAGE: list[dict[str, Any]] = []


def _note(kind: str, **fields: Any) -> None:
    TRACE.append({"t": round(time.time(), 3), "kind": kind, **fields})


def _short(value: Any, limit: int = 400) -> Any:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _install_usage_sniffer() -> None:
    """Sum the provider's own usage fields across every model call."""
    import httpx

    original = httpx.AsyncClient.send

    def _record(url: str, payload: dict[str, Any]) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            USAGE.append({"url": url, "model": payload.get("model"), "usage": usage})

    class _Tee(httpx.AsyncByteStream):
        def __init__(self, inner, url: str) -> None:
            self.inner = inner
            self.url = url
            self.buf = bytearray()
            self.parsed = False

        async def __aiter__(self):
            try:
                async for chunk in self.inner:
                    if len(self.buf) < 4_000_000:
                        self.buf.extend(chunk)
                    yield chunk
            finally:
                self._parse()

        def _parse(self) -> None:
            if self.parsed:
                return
            self.parsed = True
            last_usage = None
            model = None
            for line in bytes(self.buf).decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    continue
                try:
                    obj = json.loads(body)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                    last_usage = obj["usage"]
                    model = obj.get("model") or model
            if last_usage is not None:
                USAGE.append({"url": self.url, "model": model, "usage": last_usage, "stream": True})

        async def aclose(self) -> None:
            self._parse()
            await self.inner.aclose()

    async def send(self, request, *args, **kwargs):
        response = await original(self, request, *args, **kwargs)
        url = str(request.url)
        if "chat/completions" not in url and "generateContent" not in url:
            return response
        if kwargs.get("stream"):
            response.stream = _Tee(response.stream, url)
            return response
        try:
            await response.aread()
            _record(url, response.json())
        except Exception:  # noqa: BLE001 - telemetry must not break a call
            pass
        return response

    httpx.AsyncClient.send = send  # type: ignore[method-assign]


def _install_call_trace() -> None:
    """Record every tool decision and effect. Behavior is unchanged."""
    from app.agent import loop
    from app.cognitive import executor
    from app.ev import tools as ev_tools
    from app.fabric import policy

    classify = loop._classify

    def traced_classify(tool, arguments, *, approved):
        verdict = classify(tool, arguments, approved=approved)
        _note("agent_policy", tool=tool.spec.name, policy=verdict, approved=bool(approved),
              args=_short(arguments, 300))
        return verdict

    loop._classify = traced_classify

    execute_tool = loop._execute_tool

    async def traced_execute(tool, arguments, ctx):
        receipt = await execute_tool(tool, arguments, ctx)
        _note("agent_tool", tool=tool.spec.name, args=_short(arguments, 600),
              ok=bool(receipt.get("ok")), verified=bool(receipt.get("verified")),
              error=_short(receipt.get("error"), 300), result=_short(receipt.get("result"), 800))
        return receipt

    loop._execute_tool = traced_execute

    semantic = executor.execute_semantic

    async def traced_semantic(session, name, arguments, **kw):
        out = await semantic(session, name, arguments, **kw)
        ok = bool(out.get("ok")) if isinstance(out, dict) else None
        _note("semantic", tool=name, args=_short(arguments, 600), ok=ok,
              result=_short(out, 800))
        return out

    executor.execute_semantic = traced_semantic

    dispatch = ev_tools.dispatch

    async def traced_dispatch(session, name, args, **kw):
        out = await dispatch(session, name, args, **kw)
        _note("dispatch", tool=name, args=_short(args, 600), result=_short(
            out.model_dump() if hasattr(out, "model_dump") else out, 800))
        return out

    ev_tools.dispatch = traced_dispatch

    decide = policy.decide

    def traced_decide(action_class, target="", provenance="owner", grants=None):
        verdict = decide(action_class, target, provenance, grants)
        _note("fabric_policy", action=action_class, target=str(target)[:200],
              provenance=provenance, policy=verdict)
        return verdict

    policy.decide = traced_decide


# ------------------------------------------------------------------ worker


class Owner:
    """Plays the owner: speaks turns and answers confirmations as scripted."""

    def __init__(self, client, master_key: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {master_key}"}
        self.replies: list[dict[str, Any]] = []
        self.confirmations: list[dict[str, Any]] = []

    async def say(self, text: str, *, channel: str = "typed") -> dict[str, Any]:
        started = time.perf_counter()
        if channel == "voice":
            from app.fabric.facade import arun_kernel_turn

            result = await arun_kernel_turn(transcript=text, modality="voice",
                                            entrypoint="gauntlet_voice")
            reply = {"reply": result.spoken, "kind": result.kind}
        else:
            resp = await self.client.post("/v1/chat", headers=self.headers,
                                          json={"message": text}, timeout=600)
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            reply = {"reply": str(body.get("reply") or ""), "status": resp.status_code,
                     "model": body.get("model"),
                     "detail": body.get("detail") if resp.status_code >= 400 else None}
        reply.update({"said": text, "channel": channel,
                      "elapsed_ms": int((time.perf_counter() - started) * 1000)})
        self.replies.append(reply)
        _note("owner_turn", said=text, channel=channel, reply=_short(reply.get("reply"), 600))
        return reply

    async def pending(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            resp = await self.client.get("/v1/agent/tasks", headers=self.headers)
            body = resp.json() if resp.status_code == 200 else {}
            rows = body.get("tasks") if isinstance(body, dict) else body
            for row in rows or []:
                if isinstance(row, dict) and row.get("status") == "awaiting_approval":
                    out.append({"source": "agent", "id": row.get("id"), "tool": row.get("approval_tool"),
                                "args": row.get("approval_args")})
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = await self.client.get("/v1/runtime/actions", headers=self.headers,
                                         params={"status": "pending"})
            rows = resp.json() if resp.status_code == 200 else []
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and str(row.get("status") or "") == "pending":
                    out.append({"source": "runtime", "id": row.get("id"),
                                "tool": row.get("action_type") or row.get("tool"),
                                "args": {k: row.get(k) for k in ("action_type", "payload", "summary", "risk")}})
        except Exception:  # noqa: BLE001
            pass
        return out

    async def active_tasks(self) -> list[dict[str, Any]]:
        try:
            resp = await self.client.get("/v1/agent/tasks", headers=self.headers)
            body = resp.json() if resp.status_code == 200 else {}
            rows = body.get("tasks") if isinstance(body, dict) else body
            return [r for r in rows or [] if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            return []

    async def answer(self, item: dict[str, Any], how: str) -> None:
        """Answer one parked confirmation the way the case scripted."""
        self.confirmations.append({**item, "answer": how, "t": time.time()})
        _note("confirmation", tool=item.get("tool"), source=item.get("source"), answer=how)
        if how == "ignore":
            return
        if how in {"typed_yes", "voice_yes"}:
            await self.say("yes, go ahead", channel="voice" if how == "voice_yes" else "typed")
            return
        if how in {"typed_no", "voice_no"}:
            await self.say("no, don't", channel="voice" if how == "voice_no" else "typed")
            return
        decision = "approve" if how == "physical" else "deny"
        if item.get("source") == "agent":
            await self.client.post(f"/v1/agent/tasks/{item['id']}/decision", headers=self.headers,
                                   json={"decision": decision})
        else:
            verb = "approve" if decision == "approve" else "deny"
            resp = await self.client.post(f"/v1/runtime/actions/{item['id']}/{verb}", headers=self.headers,
                                          json={})
            _note("confirmation_result", status=resp.status_code, body=_short(resp.text, 600))

    async def settle(self, on_confirm: str, deadline: float, *, answers: int = 1) -> None:
        """Wait for background tasks. The owner answers at most ``answers`` times."""
        answered: set[str] = set()
        left = max(0, int(answers))
        quiet = 0
        while time.monotonic() < deadline:
            pend = await self.pending()
            fresh = [p for p in pend if f"{p['source']}:{p['id']}" not in answered]
            for item in fresh:
                answered.add(f"{item['source']}:{item['id']}")
                if left <= 0:
                    _note("confirmation_unanswered", tool=item.get("tool"), source=item.get("source"))
                    continue
                left -= 1
                await self.answer(item, on_confirm)
            tasks = await self.active_tasks()
            running = [t for t in tasks if t.get("status") in {"running", "queued"}]
            waiting = [t for t in tasks if t.get("status") == "awaiting_approval"]
            if not running and (not waiting or on_confirm == "ignore") and not fresh:
                quiet += 1
                if quiet >= 2:
                    return
            else:
                quiet = 0
            await asyncio.sleep(0.5)


async def _install_integrations(client, master_key: str, base_url: str) -> None:
    headers = {"Authorization": f"Bearer {master_key}"}
    specs = [
        ("messaging", ["messaging:read", "messaging:act"]),
        ("mail", ["mail:read", "mail:act"]),
        ("phone", ["phone:act"]),
        ("contacts", ["contacts:read", "contacts:act"]),
        ("calendar", ["calendar:read", "calendar:act"]),
        ("smart_home", ["home:read", "home:act"]),
    ]
    for adapter, scopes in specs:
        resp = await client.post("/v1/integrations", headers=headers, json={
            "adapter": adapter, "name": f"world {adapter}", "scopes": scopes,
            "config": {"provider": "http", "base_url": base_url},
        })
        if resp.status_code >= 300:
            _note("integration_error", adapter=adapter, status=resp.status_code, body=_short(resp.text, 300))
            continue
        integ = resp.json()
        cred = await client.post(f"/v1/integrations/{integ['id']}/credentials", headers=headers,
                                 json={"access_token": "world-token-0000", "scopes": scopes})
        if cred.status_code >= 300:
            _note("credential_error", adapter=adapter, status=cred.status_code, body=_short(cred.text, 300))


async def _run(case: dict[str, Any]) -> dict[str, Any]:
    from gauntlet.v2.fixture_server import FixtureServer, WorldState

    state = WorldState(WORLD_ROOT / "state.json", case.get("world") or {})
    server = FixtureServer(state).start()
    os.environ["GAUNTLET_FIXTURE_URL"] = server.base_url

    _install_usage_sniffer()
    import httpx

    from app.db import Base, engine
    from app.main import app

    _install_call_trace()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    master = os.environ["EV_MASTER_KEY"]
    transport = httpx.ASGITransport(app=app)
    started = time.time()
    errors: list[str] = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://world") as client:
            await _install_integrations(client, master, server.base_url)
            owner = Owner(client, master)
            limit = float(case.get("max_seconds") or 240)
            for turn in case.get("turns") or []:
                text = str(turn.get("say") or "").replace("{fixture}", server.base_url)
                try:
                    await owner.say(text, channel=str(turn.get("channel") or "typed"))
                except Exception as exc:  # noqa: BLE001 - a crashed turn is a result
                    errors.append(f"turn crashed: {type(exc).__name__}: {exc}")
                    _note("turn_error", error=_short(traceback.format_exc(), 1200))
                deadline = time.monotonic() + limit
                await owner.settle(str(turn.get("on_confirm") or case.get("on_confirm") or "ignore"),
                                   deadline, answers=int(turn.get("answers", 1)))
            tasks = await owner.active_tasks()
    server.stop()
    await engine.dispose()
    return {
        "replies": owner.replies,
        "confirmations": owner.confirmations,
        "tasks": tasks,
        "errors": errors,
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> int:
    case_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    case = json.loads(case_path.read_text(encoding="utf-8"))
    from gauntlet.v2 import tripwire

    try:
        run = asyncio.run(_run(case))
    except Exception as exc:  # noqa: BLE001 - the runner records a crashed world
        run = {"replies": [], "confirmations": [], "tasks": [],
               "errors": [f"world crashed: {type(exc).__name__}: {exc}",
                          traceback.format_exc()[-3000:]], "elapsed_s": 0}
    run["trace"] = TRACE
    run["usage"] = USAGE
    run["tripwire"] = tripwire.read_log(WORLD_ROOT)
    out_path.write_text(json.dumps(run, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
