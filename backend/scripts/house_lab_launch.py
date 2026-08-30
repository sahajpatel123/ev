"""Two consistent ASGI launches of the house/lab HTTP entry paths.

Writes full request/response traces to launch-1.log and launch-2.log.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from httpx import ASGITransport, AsyncClient


async def _exercise(client: AsyncClient, log) -> dict:
    def dump(title: str, method: str, path: str, resp, body=None) -> dict:
        payload = None
        try:
            payload = resp.json()
        except Exception:
            payload = resp.text
        record = {
            "step": title,
            "method": method,
            "path": path,
            "status": resp.status_code,
            "body": payload,
        }
        if body is not None:
            record["request"] = body
        log(json.dumps(record, indent=2, default=str))
        log("")
        return payload if isinstance(payload, dict) else {}

    created = await client.post(
        "/v1/devices",
        json={"name": "Lookout Phone", "capabilities": ["attention", "voice"]},
    )
    created_body = dump("create_device", "POST", "/v1/devices", created)
    device_id = (created_body.get("device") or {}).get("id")
    device_token = created_body.get("token")

    first = await client.get(f"/v1/devices/{device_id}/bootstrap")
    first_body = dump("bootstrap_first", "GET", f"/v1/devices/{device_id}/bootstrap", first)

    second = await client.get(f"/v1/devices/{device_id}/bootstrap")
    second_body = dump("bootstrap_second", "GET", f"/v1/devices/{device_id}/bootstrap", second)

    chat = await client.post(
        "/v1/chat", json={"message": "live thread ping", "stream": False}
    )
    chat_body = dump("chat", "POST", "/v1/chat", chat, {"message": "live thread ping"})

    transcript = await client.get("/v1/runtime/transcript")
    transcript_body = dump("transcript", "GET", "/v1/runtime/transcript", transcript)

    home_status = await client.post(
        "/v1/gateway/tools", json={"name": "home_status", "arguments": {}}
    )
    home_body = dump(
        "home_status",
        "POST",
        "/v1/gateway/tools",
        home_status,
        {"name": "home_status", "arguments": {}},
    )

    timer = await client.post(
        "/v1/gateway/tools",
        json={"name": "start_timer", "arguments": {"minutes": 37, "text": "later"}},
    )
    timer_body = dump(
        "start_timer",
        "POST",
        "/v1/gateway/tools",
        timer,
        {"name": "start_timer", "arguments": {"minutes": 37, "text": "later"}},
    )

    panic = await client.post(f"/v1/devices/{device_id}/panic")
    panic_body = dump("panic", "POST", f"/v1/devices/{device_id}/panic", panic)

    revoked_status = None
    if device_token:
        from app.main import app as asgi_app

        async with AsyncClient(
            transport=ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {device_token}"},
        ) as revoked:
            refused = await revoked.get("/v1/runtime/status")
            dump(
                "revoked_token_status",
                "GET",
                "/v1/runtime/status",
                refused,
            )
            revoked_status = refused.status_code

    spare = await client.post(
        "/v1/devices", json={"name": "Spare", "capabilities": ["attention"]}
    )
    dump("create_spare", "POST", "/v1/devices", spare)

    locked = await client.post("/v1/runtime/lock-all")
    locked_body = dump("lock_all", "POST", "/v1/runtime/lock-all", locked)

    home_result = home_body.get("result") or {}
    timer_result = timer_body.get("result") or {}
    return {
        "bootstrap_prefs": sorted((first_body.get("prefs") or {}).keys()),
        "nickname": (first_body.get("prefs") or {}).get("nickname"),
        "quiet_hours": (first_body.get("prefs") or {}).get("quiet_hours"),
        "feature_gates": bool((first_body.get("prefs") or {}).get("feature_gates") is not None),
        "spoken_first": first_body.get("spoken_text"),
        "spoken_second": second_body.get("spoken"),
        "chat_status": chat.status_code,
        "transcript_conversation": transcript_body.get("conversation_id"),
        "transcript_texts": [item.get("text") for item in transcript_body.get("events") or []],
        "home_simulated": home_result.get("simulated"),
        "home_spoken": home_result.get("spoken"),
        "home_entities": [e.get("name") for e in (home_result.get("entities") or [])],
        "timer_fire_at": timer_result.get("fire_at"),
        "timer_ok": timer_body.get("ok"),
        "panic_revoked": panic_body.get("revoked"),
        "revoked_token_status": revoked_status,
        "lock_all_count": locked_body.get("count"),
        "statuses": {
            "create": created.status_code,
            "bootstrap1": first.status_code,
            "bootstrap2": second.status_code,
            "chat": chat.status_code,
            "transcript": transcript.status_code,
            "home": home_status.status_code,
            "timer": timer.status_code,
            "panic": panic.status_code,
            "lock_all": locked.status_code,
            "revoked_token": revoked_status,
        },
    }


async def _run(log_path: Path) -> dict:
    from app.db import Base, engine
    from app.main import app

    lines: list[str] = []

    def log(text: str) -> None:
        lines.append(text)

    log(f"# House/lab ASGI launch {log_path.name}")
    log("")
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    log("schema: drop_all + create_all ok")
    log("")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    ) as client:
        summary = await _exercise(client, log)

    log("# comparable summary")
    log(json.dumps(summary, indent=2, default=str))
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


async def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    first = await _run(out_dir / "launch-1.log")
    second = await _run(out_dir / "launch-2.log")
    comparable = (
        "bootstrap_prefs",
        "nickname",
        "spoken_first",
        "spoken_second",
        "home_simulated",
        "home_spoken",
        "home_entities",
        "timer_ok",
        "panic_revoked",
        "revoked_token_status",
        "statuses",
    )
    same = all(first[key] == second[key] for key in comparable)
    print(json.dumps({"same": same, "first": first, "second": second}, indent=2, default=str))
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
