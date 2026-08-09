"""EV CLI client — scriptable, headless-friendly surface for the EV backend.

Commands mirror the v1 API: capture, ask, timeline, memories, audit, correct,
forget, restore, card, doctor, checkup, export, queue, sync. Offline captures
are queued locally with idempotency keys and replayed on ``ev sync``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
QUEUE_FILENAME = "captures.jsonl"
QUARANTINE_FILENAME = "quarantine.jsonl"


class CliError(Exception):
    """User-facing CLI failure."""


def api_url() -> str:
    return os.environ.get("EV_API_URL", DEFAULT_API_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get("EV_API_KEY", "")
    if not key:
        raise CliError("EV_API_KEY is not set (export EV_API_KEY=... before running ev)")
    return key


def queue_dir() -> Path:
    return Path(os.environ.get("EV_CLI_QUEUE_DIR", str(Path.home() / ".ev" / "queue")))


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=api_url(),
        headers={"Authorization": f"Bearer {api_key()}"},
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# Offline capture queue
# --------------------------------------------------------------------------- #


def _queue_file(queue: Path) -> Path:
    return queue / QUEUE_FILENAME


def enqueue_capture(payload: dict, idempotency_key: str, queue: Path) -> dict:
    """Persist one capture locally for later sync (offline-first)."""
    queue.mkdir(parents=True, exist_ok=True)
    record = {
        "idempotency_key": idempotency_key,
        "queued_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
    with _queue_file(queue).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return {
        "queued": True,
        "idempotency_key": idempotency_key,
        "queued_at": record["queued_at"],
    }


def list_queue(queue: Path) -> list[dict]:
    path = _queue_file(queue)
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _rewrite_queue(queue: Path, records: list[dict]) -> None:
    path = _queue_file(queue)
    if records:
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in records),
            encoding="utf-8",
        )
    else:
        path.unlink(missing_ok=True)


def _quarantine(queue: Path, record: dict, reason: str) -> None:
    queue.mkdir(parents=True, exist_ok=True)
    entry = {
        **record,
        "quarantined_at": datetime.now(UTC).isoformat(),
        "reason": reason,
    }
    with (queue / QUARANTINE_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# API commands
# --------------------------------------------------------------------------- #


async def _post_event(
    client: httpx.AsyncClient,
    payload: dict,
    idempotency_key: str,
) -> httpx.Response:
    return await client.post(
        "/v1/events",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )


async def capture(
    text: str,
    *,
    source: str = "cli",
    event_type: str = "note",
    privacy_level: str = "normal",
    device_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    queue: Path | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "source": source,
        "event_type": event_type,
        "text": text,
        "privacy_level": privacy_level,
    }
    if device_id:
        payload["device_id"] = device_id
    idempotency_key = f"cli-{uuid.uuid4()}"
    try:
        resp = await _post_event(client or _client(), payload, idempotency_key)
    except httpx.HTTPError:
        return enqueue_capture(payload, idempotency_key, queue or queue_dir())
    if resp.status_code == 201:
        return resp.json()
    if resp.status_code == 409:
        return {
            "duplicate": True,
            "event": resp.json().get("event", {}),
            "idempotency_key": idempotency_key,
        }
    raise CliError(f"capture failed ({resp.status_code}): {resp.text[:500]}")


async def sync_captures(
    client: httpx.AsyncClient,
    queue: Path,
) -> dict:
    """Replay the local offline queue; 201 = synced, 409 = duplicate dropped."""
    records = list_queue(queue)
    if not records:
        return {"synced": 0, "dropped": 0, "quarantined": 0, "errors": [], "remaining": 0}
    synced = 0
    dropped = 0
    quarantined = 0
    remaining: list[dict] = []
    errors: list[str] = []
    for record in records:
        key = record["idempotency_key"]
        try:
            resp = await _post_event(client, record["payload"], key)
        except httpx.HTTPError as exc:
            remaining.append(record)
            errors.append(f"{key}: {exc}")
            break
        if resp.status_code == 201:
            synced += 1
        elif resp.status_code == 409:
            dropped += 1
        elif resp.status_code in (400, 422):
            quarantined += 1
            _quarantine(queue, record, resp.text[:500])
        else:
            remaining.append(record)
            errors.append(f"{key}: HTTP {resp.status_code} {resp.text[:200]}")
    _rewrite_queue(queue, remaining)
    return {
        "synced": synced,
        "dropped": dropped,
        "quarantined": quarantined,
        "errors": errors,
        "remaining": len(remaining),
    }


async def ask(
    question: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 120.0,
) -> dict:
    c = client or _client(timeout)
    resp = await c.post("/v1/chat", json={"message": question, "stream": False})
    if resp.status_code != 200:
        raise CliError(f"ask failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def timeline(
    *,
    limit: int = 50,
    cursor: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if source:
        params["source"] = source
    if event_type:
        params["event_type"] = event_type
    c = client or _client()
    resp = await c.get("/v1/timeline", params=params)
    if resp.status_code != 200:
        raise CliError(f"timeline failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def memories(
    *,
    memory_type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    client: httpx.AsyncClient | None = None,
) -> dict:
    params: dict[str, Any] = {"limit": limit}
    if memory_type:
        params["memory_type"] = memory_type
    if q:
        params["q"] = q
    c = client or _client()
    resp = await c.get("/v1/memories", params=params)
    if resp.status_code != 200:
        raise CliError(f"memories failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def audit(
    memory_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get(f"/v1/audit/{memory_id}")
    if resp.status_code != 200:
        raise CliError(f"audit failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def correct(
    memory_id: str,
    corrected_text: str,
    *,
    reason: str = "user correction",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(
        f"/v1/memories/{memory_id}/correct",
        json={"corrected_text": corrected_text, "reason": reason},
    )
    if resp.status_code not in (200, 201):
        raise CliError(f"correct failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def forget(
    memory_id: str,
    *,
    reason: str = "user requested",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(
        f"/v1/memories/{memory_id}/forget",
        json={"reason": reason},
    )
    if resp.status_code != 200:
        raise CliError(f"forget failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def restore(
    memory_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(f"/v1/memories/{memory_id}/restore")
    if resp.status_code != 200:
        raise CliError(f"restore failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def card(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/hud/card")
    if resp.status_code != 200:
        raise CliError(f"card failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def quickcard(
    topic: str,
    *,
    stakes: str | None = None,
    context: str | None = None,
    ttl_seconds: int = 3600,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """HUD tactical quick card (ev.hud.quickcard.v1), cached < 800 ms reads."""
    params: dict[str, Any] = {"topic": topic, "ttl_seconds": ttl_seconds}
    if stakes:
        params["stakes"] = stakes
    if context:
        params["context"] = context
    c = client or _client()
    resp = await c.get("/v1/tactical/quick", params=params)
    if resp.status_code != 200:
        raise CliError(f"quickcard failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def doctor(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/health")
    if resp.status_code != 200:
        raise CliError(f"doctor failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def checkup(
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 120.0,
) -> dict:
    c = client or _client(timeout)
    resp = await c.post("/v1/diagnostics/calibrate")
    if resp.status_code != 200:
        raise CliError(f"checkup failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def export_bundle(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post("/v1/export")
    if resp.status_code != 200:
        raise CliError(f"export failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def import_bundle_file(
    path: str | Path,
    *,
    mode: str = "merge",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Import an export bundle (event-sourced restore/merge)."""
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    c = client or _client()
    resp = await c.post("/v1/import", json=bundle, params={"mode": mode})
    if resp.status_code != 200:
        raise CliError(f"import failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def onboarding(
    texts: list[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Guided first memories: capture N initial memories, then show their audit."""
    c = client or _client()
    events: list[dict] = []
    audits: list[dict] = []
    for text in texts:
        text = text.strip()
        if not text:
            continue
        resp = await c.post(
            "/v1/events",
            json={"source": "onboarding", "event_type": "note", "text": text},
            headers={"Idempotency-Key": f"onboarding-{uuid.uuid4()}"},
        )
        if resp.status_code != 201:
            raise CliError(f"onboarding capture failed ({resp.status_code}): {resp.text[:500]}")
        event = resp.json()["event"]
        events.append(event)
        search = await c.get("/v1/memories", params={"q": text, "limit": 1})
        if search.status_code == 200 and search.json().get("memories"):
            memory = search.json()["memories"][0]
            audit_resp = await c.get(f"/v1/audit/{memory['id']}")
            if audit_resp.status_code == 200:
                audits.append(audit_resp.json())
    return {"events": events, "audits": audits}


# --------------------------------------------------------------------------- #
# Output formatting + CLI entrypoint
# --------------------------------------------------------------------------- #


def _event_text(event: dict) -> str:
    content = event.get("content") or {}
    return str(content.get("text") or event.get("metadata", {}).get("text") or "")


def _print_timeline(data: dict) -> None:
    for event in data.get("events", []):
        print(
            f"{event['occurred_at']}  [{event['source']}/{event['event_type']}]  "
            f"{_event_text(event)[:120]}  ({event['id']})"
        )
    if data.get("next_cursor"):
        print(f"next_cursor: {data['next_cursor']}")


def _print_memories(data: dict) -> None:
    for memory in data.get("memories", []):
        print(
            f"{memory['created_time']}  {memory['memory_type']} v{memory['version']}  "
            f"conf={memory['confidence']}  {memory['text'][:120]}  ({memory['id']})"
        )
    print(f"total: {data.get('total', len(data.get('memories', [])))}")


def _print_audit(data: dict) -> None:
    memory = data["memory"]
    print(f"{memory['memory_type']} v{memory['version']} — {memory['text']}")
    versions = " -> ".join(str(v["version"]) for v in data.get("versions", []))
    print(f"versions: {versions}")
    for event in data.get("source_events", []):
        print(f"  source: {event['occurred_at']} [{event['source']}/{event['event_type']}] {_event_text(event)[:120]}")
    for conflict in data.get("conflicts", []):
        print(f"  conflict ({conflict['status']}): {conflict['reason']}")
    print(f"access log entries: {len(data.get('access_log', []))}")


async def _run(args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "capture":
        text = args.text or sys.stdin.read().strip()
        if not text:
            raise CliError("nothing to capture: pass text or pipe it on stdin")
        async with _client() as client:
            result = await capture(
                text,
                source=args.source,
                event_type=args.event_type,
                privacy_level=args.privacy,
                device_id=args.device_id,
                client=client,
            )
        if result.get("queued"):
            print(
                f"EV is offline — capture queued ({result['idempotency_key']}). "
                "Run `ev sync` to send it."
            )
        else:
            event = result.get("event", {})
            print(f"captured {event.get('id')}")
            for delta in result.get("memory_delta", []):
                print(f"  memory {delta['action']}: {delta['memory_type']} — {delta['text'][:100]}")
        return 0
    if cmd == "ask":
        async with _client(120.0) as client:
            result = await ask(args.question, client=client)
        print(result["reply"])
        for item in result.get("provenance", []):
            print(f"  [source {item['memory_type']} score={item['score']}] {item['text'][:100]}")
        return 0
    if cmd == "timeline":
        async with _client() as client:
            data = await timeline(
                limit=args.limit,
                cursor=args.cursor,
                source=args.source,
                event_type=args.event_type,
                client=client,
            )
        _print_timeline(data)
        return 0
    if cmd == "memories":
        async with _client() as client:
            data = await memories(
                memory_type=args.type,
                q=args.search,
                limit=args.limit,
                client=client,
            )
        _print_memories(data)
        return 0
    if cmd == "audit":
        async with _client() as client:
            data = await audit(args.memory_id, client=client)
        _print_audit(data)
        return 0
    if cmd == "correct":
        async with _client() as client:
            memory = await correct(
                args.memory_id,
                args.text,
                reason=args.reason,
                client=client,
            )
        print(f"corrected v{memory['version']}: {memory['text']}")
        return 0
    if cmd == "forget":
        async with _client() as client:
            memory = await forget(args.memory_id, reason=args.reason, client=client)
        print(f"forgot v{memory['version']}: {memory['text']}")
        return 0
    if cmd == "restore":
        async with _client() as client:
            memory = await restore(args.memory_id, client=client)
        print(f"restored v{memory['version']}: {memory['text']}")
        return 0
    if cmd == "card":
        async with _client() as client:
            hud = await card(client=client)
        print(f"[{hud['schema_version']}] {hud['title']} (priority {hud['priority']})")
        print(hud["body"])
        return 0
    if cmd == "quickcard":
        async with _client() as client:
            hud = await quickcard(
                args.topic,
                stakes=args.stakes,
                context=args.context,
                ttl_seconds=args.ttl,
                client=client,
            )
        print(f"[{hud['schema_version']}] {hud['objective']}")
        print(hud["summary"])
        parts = []
        if hud.get("next_action"):
            parts.append(f"next: {hud['next_action']}")
        if hud.get("top_risk"):
            parts.append(f"risk: {hud['top_risk']}")
        parts.append(
            f"people {hud.get('people_count', 0)} · options {hud.get('options_count', 0)} "
            f"· history {hud.get('decision_history_count', 0)}"
        )
        print(" | ".join(parts))
        return 0
    if cmd == "doctor":
        async with _client() as client:
            health = await doctor(client=client)
        print(f"status: {health['status']}  app: {health['app']}  env: {health['environment']}")
        print(f"version: {health['version']}")
        print(f"capabilities ({len(health['capabilities'])}): {', '.join(health['capabilities'])}")
        return 0
    if cmd == "checkup":
        async with _client(120.0) as client:
            report = await checkup(client=client)
        print(json.dumps(report, indent=2, default=str))
        return 0
    if cmd == "export":
        async with _client() as client:
            bundle = await export_bundle(client=client)
        text = json.dumps(bundle, indent=2, default=str)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
            print(f"exported {len(bundle['events'])} events, {len(bundle['memories'])} memories -> {args.output}")
        else:
            print(text)
        return 0
    if cmd == "import":
        async with _client() as client:
            summary = await import_bundle_file(
                args.file,
                mode=args.mode,
                client=client,
            )
        print(
            f"imported {summary['events_imported']} events "
            f"({summary['events_skipped']} skipped) -> "
            f"{summary['memories_created']} memories, "
            f"{summary['patterns_created']} patterns, "
            f"{summary['summaries_created']} summaries, "
            f"{summary['lessons_created']} lessons"
        )
        return 0
    if cmd == "onboarding":
        texts = list(args.texts or [])
        if not texts:
            print("Tell EV three things you want it to remember.")
            for _ in range(3):
                line = input("> ").strip()
                if line:
                    texts.append(line)
        if not texts:
            raise CliError("nothing to remember")
        async with _client() as client:
            result = await onboarding(texts, client=client)
        print(f"EV remembers {len(result['events'])} things.")
        for trail in result["audits"]:
            memory = trail["memory"]
            sources = len(trail["source_events"])
            print(
                f"  audit: {memory['memory_type']} v{memory['version']} — "
                f"{memory['text'][:80]} ({sources} source events)"
            )
        return 0
    if cmd == "queue":
        records = list_queue(queue_dir())
        if not records:
            print("queue is empty")
            return 0
        for record in records:
            payload = record["payload"]
            print(f"{record['queued_at']}  {record['idempotency_key']}  {payload.get('text', '')[:120]}")
        return 0
    if cmd == "sync":
        async with _client() as client:
            summary = await sync_captures(client, queue_dir())
        print(
            f"synced {summary['synced']}, duplicates dropped {summary['dropped']}, "
            f"quarantined {summary['quarantined']}, remaining {summary['remaining']}"
        )
        for error in summary["errors"]:
            print(f"  error: {error}", file=sys.stderr)
        return 0
    raise CliError(f"unknown command: {cmd}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ev",
        description="EV — personal AI companion CLI (capture, ask, memory, HUD).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_capture = sub.add_parser("capture", help="remember something (text or stdin)")
    p_capture.add_argument("text", nargs="?", default=None, help="text to capture; omit to read stdin")
    p_capture.add_argument("--source", default="cli", help="event source (default: cli)")
    p_capture.add_argument("--event-type", default="note", help="event type (default: note)")
    p_capture.add_argument("--privacy", default="normal", choices=["private", "normal", "sensitive", "never_send_to_model"])
    p_capture.add_argument("--device-id", default=None, help="device identifier")

    p_ask = sub.add_parser("ask", help="ask EV a question")
    p_ask.add_argument("question")

    p_timeline = sub.add_parser("timeline", help="recent events")
    p_timeline.add_argument("--limit", type=int, default=50)
    p_timeline.add_argument("--cursor", default=None)
    p_timeline.add_argument("--source", default=None)
    p_timeline.add_argument("--event-type", default=None)

    p_memories = sub.add_parser("memories", help="browse memories")
    p_memories.add_argument("--type", default=None, help="memory type (decision, goal, ...)")
    p_memories.add_argument("--search", default=None, help="semantic search query")
    p_memories.add_argument("--limit", type=int, default=50)

    p_audit = sub.add_parser("audit", help="why does EV know this?")
    p_audit.add_argument("memory_id")

    p_correct = sub.add_parser("correct", help="correct a memory")
    p_correct.add_argument("memory_id")
    p_correct.add_argument("text")
    p_correct.add_argument("--reason", default="user correction")

    p_forget = sub.add_parser("forget", help="forget a memory")
    p_forget.add_argument("memory_id")
    p_forget.add_argument("--reason", default="user requested")

    p_restore = sub.add_parser("restore", help="restore a forgotten memory")
    p_restore.add_argument("memory_id")

    sub.add_parser("card", help="render the ev.hud.card.v1 status card")
    p_quickcard = sub.add_parser(
        "quickcard",
        help="render the ev.hud.quickcard.v1 tactical quick card",
    )
    p_quickcard.add_argument("topic", help="briefing topic, e.g. 'Renegotiation with X'")
    p_quickcard.add_argument("--stakes", default=None, help="stakes context")
    p_quickcard.add_argument("--context", default=None, help="extra context")
    p_quickcard.add_argument("--ttl", type=int, default=3600, help="cache TTL seconds")
    sub.add_parser("doctor", help="EV health check")
    sub.add_parser("checkup", help="run full diagnostics/calibration")

    p_export = sub.add_parser("export", help="export all events + memories as JSON")
    p_export.add_argument("--output", default=None, help="write to file instead of stdout")

    p_import = sub.add_parser("import", help="import an export bundle (merge or replace)")
    p_import.add_argument("file", help="path to an exported JSON bundle")
    p_import.add_argument(
        "--mode",
        default="merge",
        choices=["merge", "replace"],
        help="replace only works against an empty event log (fresh restore)",
    )

    p_onboarding = sub.add_parser("onboarding", help="guided first memories + first audit")
    p_onboarding.add_argument("texts", nargs="*", help="initial memories (interactive if omitted)")

    sub.add_parser("queue", help="list queued offline captures")
    sub.add_parser("sync", help="send queued offline captures")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
