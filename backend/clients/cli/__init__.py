"""EV CLI client — scriptable, headless-friendly surface for the EV backend.

Commands mirror the v1 API: capture, ask, timeline, memories, audit, correct,
forget, restore, card, doctor, checkup, export, queue, sync. Offline captures
are queued locally with idempotency keys and replayed on ``ev sync``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
QUEUE_FILENAME = "captures.jsonl"
QUARANTINE_FILENAME = "quarantine.jsonl"
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class CliError(Exception):
    """User-facing CLI failure."""


def api_url() -> str:
    return os.environ.get("EV_API_URL", DEFAULT_API_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get("EV_API_KEY", "")
    if not key:
        raise CliError("EV_API_KEY is not set (export EV_API_KEY=... before running ev)")
    return key


def workbench_info() -> dict:
    """Workbench URL + whether loopback auto-connect should kick in."""
    url = api_url()
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].strip("[]").lower()
    return {
        "url": f"{url}/app",
        "loopback_auto_connect": host in ("127.0.0.1", "localhost", "::1"),
    }


def queue_dir() -> Path:
    return Path(os.environ.get("EV_CLI_QUEUE_DIR", str(Path.home() / ".ev" / "queue")))


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=api_url(),
        headers={"Authorization": f"Bearer {api_key()}"},
        timeout=timeout,
    )


async def _iter_sse(resp: Any):
    """Parse ``event:``/``data:`` frames from an httpx streaming response."""
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                payload = json.loads("\n".join(data_lines))
                yield event_name or "message", payload
                event_name = None
                data_lines = []
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        payload = json.loads("\n".join(data_lines))
        yield event_name or "message", payload


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


def enqueue_attachment(
    payload: dict,
    file_path: Path,
    idempotency_key: str,
    queue: Path,
) -> dict:
    """Persist one attachment capture locally for later sync."""
    queue.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": "attachment",
        "idempotency_key": idempotency_key,
        "queued_at": datetime.now(UTC).isoformat(),
        "payload": payload,
        "file_path": str(file_path),
    }
    with _queue_file(queue).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return {
        "queued": True,
        "idempotency_key": idempotency_key,
        "queued_at": record["queued_at"],
        "file": str(file_path),
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


async def attach(
    path: str | Path,
    *,
    source: str = "attachment",
    event_type: str = "file",
    privacy_level: str = "normal",
    device_id: str | None = None,
    metadata: dict | None = None,
    client: httpx.AsyncClient | None = None,
    queue: Path | None = None,
) -> dict:
    """Capture a file/share as an attachment event (multipart upload)."""
    file_path = Path(path)
    if not file_path.is_file():
        raise CliError(f"file not found: {path}")
    c = client or _client(120.0)
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    data: dict[str, Any] = {
        "source": source,
        "event_type": event_type,
        "privacy_level": privacy_level,
        "metadata": json.dumps(metadata or {}),
    }
    if device_id:
        data["device_id"] = device_id
    try:
        with file_path.open("rb") as fh:
            resp = await c.post(
                "/v1/attachments",
                data=data,
                files={"file": (file_path.name, fh, content_type)},
            )
    except httpx.HTTPError:
        return enqueue_attachment(
            data,
            file_path,
            f"cli-{uuid.uuid4()}",
            queue or queue_dir(),
        )
    if resp.status_code != 201:
        raise CliError(f"attach failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def _post_attachment(client: httpx.AsyncClient, record: dict) -> httpx.Response:
    path = Path(record["file_path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = dict(record.get("payload", {}))
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return await client.post(
            "/v1/attachments",
            data=payload,
            files={"file": (path.name, fh, content_type)},
        )


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
            if record.get("kind") == "attachment":
                resp = await _post_attachment(client, record)
            else:
                resp = await _post_event(client, record["payload"], key)
        except FileNotFoundError as exc:
            quarantined += 1
            _quarantine(queue, record, f"attachment file missing: {exc}")
            continue
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


async def list_protocols(
    *,
    client: httpx.AsyncClient | None = None,
    include_refused: bool = True,
) -> dict:
    c = client or _client()
    resp = await c.get(
        "/v1/assistant/protocols",
        params={"include_refused": str(include_refused).lower()},
    )
    if resp.status_code != 200:
        raise CliError(f"protocols failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


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


async def present(
    title: str,
    body: str,
    *,
    kind: str = "card",
    size: str | None = None,
    time_type: str | None = None,
    placement: str | None = None,
    ttl_ms: int | None = None,
    lookout: bool = False,
    auto: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    payload = {"title": title, "body": body, "kind": kind, "lookout": lookout, "auto": auto}
    if size:
        payload["size"] = size
    if time_type:
        payload["time_type"] = time_type
    if placement:
        payload["placement"] = placement
    if ttl_ms is not None:
        payload["ttl_ms"] = ttl_ms
    resp = await c.post(
        "/v1/runtime/present",
        json=payload,
    )
    if resp.status_code != 200:
        raise CliError(f"present failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def ask_stream(
    question: str,
    *,
    conversation_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    on_delta=None,
    on_refined=None,
    on_provenance=None,
) -> dict:
    """Stream an answer from POST /v1/chat (SSE): delta/refined/provenance/done.

    ``on_delta(text)`` receives progressive tokens, ``on_refined(text)`` the
    output-filtered final answer, and ``on_provenance(item)`` each source
    chip. Returns the ``done`` payload.
    """

    c = client or _client(120.0)
    body: dict[str, Any] = {"message": question, "stream": True}
    if conversation_id:
        body["conversation_id"] = conversation_id
    async with c.stream("POST", "/v1/chat", json=body) as resp:
        if resp.status_code != 200:
            raise CliError(f"ask failed ({resp.status_code}): {resp.text[:500]}")
        done: dict = {}
        async for event, data in _iter_sse(resp):
            if event == "delta":
                if callable(on_delta):
                    on_delta(str(data.get("text", "")))
            elif event == "refined":
                if callable(on_refined):
                    on_refined(str(data.get("text", "")))
            elif event == "provenance":
                if callable(on_provenance):
                    on_provenance(data)
            elif event == "error":
                raise CliError(f"stream error: {data.get('message', 'unknown error')}")
            elif event == "done":
                done = data
        return done


async def voice_wake(
    *,
    device_id: str = "cli",
    wake_word: str = "evie",
    priority: float = 0.5,
    text_hint: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    body: dict[str, Any] = {
        "device_id": device_id,
        "wake_word": wake_word,
        "priority": priority,
    }
    if text_hint:
        body["text_hint"] = text_hint
    c = client or _client()
    resp = await c.post("/v1/voice/wake", json=body)
    if resp.status_code != 201:
        raise CliError(f"voice wake failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def voice_session_verify(
    session_id: str,
    samples: list[str | Path],
    *,
    nonce: str | None = None,
    phrase: str | None = None,
    liveness: str = "live",
    live_score: float = 0.9,
    client: httpx.AsyncClient | None = None,
) -> dict:
    encoded: list[str] = []
    for sample in samples:
        path = Path(sample)
        if not path.is_file():
            raise CliError(f"sample file not found: {sample}")
        encoded.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    if not encoded:
        raise CliError("at least one sample file is required")
    body: dict[str, Any] = {
        "session_id": session_id,
        "nonce": nonce or "cli-verify",
        "samples": encoded,
        "liveness_proof": liveness,
        "live_score": live_score,
    }
    if phrase:
        body["phrase"] = phrase
    c = client or _client(120.0)
    resp = await c.post("/v1/voice/verify", json=body)
    if resp.status_code != 200:
        raise CliError(f"voice verify failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def voice_listen(
    session_id: str,
    *,
    text: str | None = None,
    audio: str | Path | None = None,
    conversation_id: str | None = None,
    follow_up: bool = False,
    client: httpx.AsyncClient | None = None,
    on_partial=None,
    on_final=None,
) -> dict:
    """Stream a voice utterance: partial ASR, final transcript, then reply."""
    body: dict[str, Any] = {"session_id": session_id, "follow_up": follow_up}
    if text:
        body["text"] = text
    if audio:
        path = Path(audio)
        if not path.is_file():
            raise CliError(f"audio file not found: {audio}")
        body["audio_b64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    if conversation_id:
        body["conversation_id"] = conversation_id
    c = client or _client(120.0)
    async with c.stream("POST", "/v1/voice/utterance/stream", json=body) as resp:
        if resp.status_code != 200:
            raise CliError(f"voice listen failed ({resp.status_code}): {resp.text[:500]}")
        result: dict = {}
        async for event, data in _iter_sse(resp):
            if event == "partial":
                if callable(on_partial):
                    on_partial(data)
            elif event == "final_transcript":
                if callable(on_final):
                    on_final(data)
            elif event == "reply":
                result = data
            elif event == "error":
                code = data.get("code", "voice_error")
                raise CliError(f"voice error ({code}): {data.get('message', 'unknown error')}")
            elif event == "done":
                break
        return result


async def voice_status(
    session_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get(f"/v1/voice/sessions/{session_id}")
    if resp.status_code != 200:
        raise CliError(f"voice status failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def voice_end(
    session_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(f"/v1/voice/sessions/{session_id}/end")
    if resp.status_code != 200:
        raise CliError(f"voice end failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def notify_send(
    title: str,
    body: str,
    *,
    priority: float = 0.5,
    tier: str = "useful",
    kind: str = "manual",
    source: str | None = None,
    emergency: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "priority": priority,
        "tier": tier,
        "kind": kind,
    }
    if source:
        payload["source"] = source
    if emergency:
        payload["emergency"] = emergency
    c = client or _client()
    resp = await c.post("/v1/runtime/notify", json=payload)
    if resp.status_code != 201:
        raise CliError(f"notify failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def notify_history(
    *,
    status: str | None = None,
    limit: int = 50,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    c = client or _client()
    resp = await c.get("/v1/runtime/notifications", params=params)
    if resp.status_code != 200:
        raise CliError(f"notifications failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def notify_status_report(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/runtime/notify/status")
    if resp.status_code != 200:
        raise CliError(f"notify status failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def model_list(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/gateway/models")
    if resp.status_code != 200:
        raise CliError(f"model list failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def _run_model_cli(args: list[str]) -> int:
    """Delegate model cache operations to Agent 2's local model CLI."""
    return subprocess.run(
        [sys.executable, "-m", "app.ml.cli", *args],
        cwd=str(BACKEND_ROOT),
        check=False,
    ).returncode


def model_pull(name: str) -> int:
    return _run_model_cli(["pull", name])


def model_prune(*, all_files: bool = False, dry_run: bool = False) -> int:
    args = ["prune"]
    if all_files:
        args.append("--all")
    if dry_run:
        args.append("--dry-run")
    return _run_model_cli(args)


def model_stats() -> int:
    """Print Agent 2's arbiter stats (ceiling, resident MB, backend, disk)."""
    return _run_model_cli(["stats"])


async def people_enroll(
    person_name: str,
    photos: list[str | Path],
    *,
    quality: float = 0.99,
    confidence: float = 0.99,
    source: str = "cli",
    reason: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    if len(photos) < 5:
        raise CliError("face enrollment needs at least 5 photos")
    encoded: list[dict] = []
    for photo in photos:
        path = Path(photo)
        if not path.is_file():
            raise CliError(f"photo file not found: {photo}")
        encoded.append(
            {
                "image_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "quality": quality,
                "confidence": confidence,
                "source": source,
            }
        )
    body: dict[str, Any] = {"person_name": person_name, "photos": encoded}
    if reason:
        body["reason"] = reason
    c = client or _client(120.0)
    resp = await c.post("/v1/people/enrollments", json=body)
    if resp.status_code == 403:
        raise CliError("consent required: run `ev consent grant face_enrollment` first")
    if resp.status_code != 201:
        raise CliError(f"people enroll failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def people_list(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    c = client or _client()
    resp = await c.get("/v1/people/enrollments")
    if resp.status_code != 200:
        raise CliError(f"people list failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def people_forget(
    entity_id: str,
    *,
    reason: str = "user requested person deletion",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.delete(f"/v1/people/{entity_id}", params={"reason": reason})
    if resp.status_code != 200:
        raise CliError(f"people forget failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def people_correct(
    recognition_id: str,
    correct_label: str,
    *,
    reason: str = "cli correction",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(
        f"/v1/people/recognitions/{recognition_id}/confirm",
        json={"correct_label": correct_label, "reason": reason},
    )
    if resp.status_code != 200:
        raise CliError(f"people correct failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def train_dry_run(
    corpus_version: int,
    *,
    provider: str = "local-lora",
    base_model: str | None = None,
    adapter_ref: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    body: dict[str, Any] = {"corpus_version": corpus_version, "provider": provider}
    if base_model:
        body["base_model"] = base_model
    if adapter_ref:
        body["adapter_ref"] = adapter_ref
    c = client or _client(120.0)
    resp = await c.post("/v1/training/adapter/dry-run", json=body)
    if resp.status_code == 403:
        raise CliError("consent required: run `ev consent grant adapter_fine_tuning` first")
    if resp.status_code != 200:
        raise CliError(f"train dry-run failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def train_status(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    c = client or _client()
    resp = await c.get("/v1/training/adapter")
    if resp.status_code != 200:
        raise CliError(f"train status failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def train_rollback(
    adapter_id: str,
    *,
    reason: str = "rollback adapter",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(
        "/v1/training/adapter/rollback",
        json={"adapter_id": adapter_id, "reason": reason},
    )
    if resp.status_code != 200:
        raise CliError(f"train rollback failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def eval_retrieval(
    *,
    k: int = 10,
    rerank: bool = False,
    questions: str | Path | None = None,
    out: str | Path | None = None,
    database_url: str | None = None,
) -> dict:
    """Run Agent 8's retrieval harness (synthetic corpus or live DB)."""
    args = [
        sys.executable,
        "-m",
        "eval.retrieval.cli",
        "retrieval",
        "--k",
        str(k),
    ]
    if rerank:
        args.append("--rerank")
    if questions:
        args += ["--questions", str(questions)]
    if out:
        args += ["--out", str(out)]
    if database_url:
        args += ["--database-url", database_url]
    completed = subprocess.run(args, cwd=str(BACKEND_ROOT), check=False)
    if completed.returncode != 0:
        raise CliError("retrieval eval failed (see output above)")
    if out:
        path = Path(out)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {"returncode": 0, "note": "report printed to stdout"}


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word error rate (Levenshtein over whitespace tokens), 0..1."""
    ref = str(reference).strip().split()
    hyp = str(hypothesis).strip().split()
    if not ref:
        return 1.0 if hyp else 0.0
    previous = list(range(len(hyp) + 1))
    for row_index, ref_word in enumerate(ref, start=1):
        current = [row_index] + [0] * len(hyp)
        for col_index, hyp_word in enumerate(hyp, start=1):
            cost = 0 if ref_word.lower() == hyp_word.lower() else 1
            current[col_index] = min(
                previous[col_index] + 1,
                current[col_index - 1] + 1,
                previous[col_index - 1] + cost,
            )
        previous = current
    return round(previous[-1] / len(ref), 4)


async def eval_asr(
    *,
    audio: str | Path | None = None,
    expected: str | None = None,
    language: str = "en",
) -> dict:
    """Self-probe the configured ASR factory: transcript, confidence, degraded."""
    try:
        from app.voice.asr import get_transcriber
        from app.voice.contracts import VoiceError
    except Exception as exc:  # noqa: BLE001 - backend settings may be unset
        return {
            "provider": "unavailable",
            "status": "error",
            "code": "settings_unavailable",
            "message": str(exc),
            "note": "backend settings require EV_MASTER_KEY/EV_VAULT_KEY; run from the backend env",
        }

    transcriber = get_transcriber()
    kwargs: dict[str, Any] = {"language": language}
    if audio:
        path = Path(audio)
        if not path.is_file():
            raise CliError(f"audio file not found: {audio}")
        kwargs["audio_b64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    else:
        kwargs["text_hint"] = "EVIE evaluation phrase: remember local AI tools."
    try:
        result = await transcriber.transcribe(**kwargs)
    except VoiceError as exc:
        return {
            "provider": getattr(transcriber, "name", type(transcriber).__name__),
            "status": "error",
            "code": exc.code,
            "message": exc.message,
            "note": "configure a real ASR provider (EV_VOICE_ASR_PROVIDER) for WER evals",
        }
    exact = (
        expected is not None
        and result.text.strip().lower() == str(expected).strip().lower()
    )
    wer = word_error_rate(expected, result.text) if expected is not None else None
    return {
        "provider": result.provider,
        "transcript": result.text,
        "confidence": result.confidence,
        "degraded": result.degraded,
        "expected": expected,
        "exact_match": exact,
        "wer": wer,
        "note": (
            "dev double echoes hints; configure a real ASR provider for WER evals"
            if result.provider == "echo"
            else ""
        ),
    }


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


async def identity_status(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/identity/status")
    if resp.status_code != 200:
        raise CliError(f"identity status failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def identity_owner_create(
    display_name: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post("/v1/identity/owner", json={"display_name": display_name})
    if resp.status_code != 201:
        raise CliError(f"owner creation failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def identity_passkey_add(
    credential_id: str,
    name: str,
    *,
    device_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    payload: dict[str, Any] = {"credential_id": credential_id, "name": name}
    if device_id:
        payload["device_id"] = device_id
    resp = await c.post("/v1/identity/passkeys", json=payload)
    if resp.status_code != 201:
        raise CliError(f"passkey registration failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def identity_passkey_list(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    c = client or _client()
    resp = await c.get("/v1/identity/passkeys")
    if resp.status_code != 200:
        raise CliError(f"passkey list failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def identity_passkey_remove(
    passkey_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.delete(f"/v1/identity/passkeys/{passkey_id}")
    if resp.status_code != 200:
        raise CliError(f"passkey revocation failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def identity_recovery_redeem(
    code: str,
    device_name: str,
    *,
    capabilities: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Recovery is deliberately unauthenticated: no EV_API_KEY is required."""
    payload = {
        "code": code,
        "device_name": device_name,
        "capabilities": capabilities or [],
    }
    if client is None:
        async with httpx.AsyncClient(base_url=api_url(), timeout=30.0) as anon:
            resp = await anon.post("/v1/identity/recovery/redeem", json=payload)
    else:
        resp = await client.post("/v1/identity/recovery/redeem", json=payload)
    if resp.status_code != 201:
        raise CliError(f"recovery redeem failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def identity_reverification_issue(
    purpose: str,
    *,
    session_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    payload: dict[str, Any] = {"purpose": purpose}
    if session_id:
        payload["voice_session_id"] = session_id
    resp = await c.post("/v1/identity/reverification", json=payload)
    if resp.status_code != 200:
        raise CliError(f"re-verification failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def consent_grant(
    track: str,
    *,
    purpose: str = "personalize EV to the owner",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post("/v1/training/consent", json={"track": track, "purpose": purpose})
    if resp.status_code not in (200, 201):
        raise CliError(f"consent grant failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def consent_list(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    c = client or _client()
    resp = await c.get("/v1/training/consent")
    if resp.status_code != 200:
        raise CliError(f"consent list failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def consent_revoke(
    track: str,
    *,
    reason: str = "user revoked",
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.post(f"/v1/training/consent/{track}/revoke", json={"reason": reason})
    if resp.status_code != 200:
        raise CliError(f"consent revoke failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def voice_enroll(
    samples: list[str | Path],
    *,
    liveness: str | None = None,
    live_score: float | None = None,
    reason: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Enroll the owner voiceprint from audio sample files (base64 upload)."""
    encoded: list[str] = []
    for sample in samples:
        path = Path(sample)
        if not path.is_file():
            raise CliError(f"sample file not found: {sample}")
        encoded.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    if len(encoded) < 5:
        raise CliError("voice enrollment needs at least 5 sample files")
    body: dict[str, Any] = {"samples": encoded}
    if liveness:
        body["liveness_proof"] = liveness
    if live_score is not None:
        body["live_score"] = live_score
    if reason:
        body["reason"] = reason
    c = client or _client(120.0)
    resp = await c.post("/v1/training/voice/enroll", json=body)
    if resp.status_code == 403:
        raise CliError("consent required: run `ev consent grant voice_enrollment` first")
    if resp.status_code != 201:
        raise CliError(f"voice enroll failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def voice_verify(
    samples: list[str | Path],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Verify the owner voiceprint from audio sample files."""
    encoded: list[str] = []
    for sample in samples:
        path = Path(sample)
        if not path.is_file():
            raise CliError(f"sample file not found: {sample}")
        encoded.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    if not encoded:
        raise CliError("at least one sample file is required")
    c = client or _client(120.0)
    resp = await c.post("/v1/training/voice/verify", json={"samples": encoded})
    if resp.status_code != 200:
        raise CliError(f"voice verify failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def routines_overview(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/routines/overview")
    if resp.status_code != 200:
        raise CliError(f"routines overview failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def list_routines(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    c = client or _client()
    resp = await c.get("/v1/routines")
    if resp.status_code != 200:
        raise CliError(f"routines list failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def list_routine_templates(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    c = client or _client()
    resp = await c.get("/v1/routines/templates")
    if resp.status_code != 200:
        raise CliError(f"routines templates failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def run_routine(
    routine_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client(120.0)
    resp = await c.post(f"/v1/routines/{routine_id}/run")
    if resp.status_code != 201:
        raise CliError(f"routine run failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def create_routine(
    name: str,
    action_type: str,
    *,
    schedule: str | None = None,
    kind: str = "scheduled",
    client: httpx.AsyncClient | None = None,
) -> dict:
    body: dict[str, Any] = {"name": name, "kind": kind, "action_type": action_type}
    if schedule:
        body["schedule"] = schedule
    c = client or _client()
    resp = await c.post("/v1/routines", json=body)
    if resp.status_code != 201:
        raise CliError(f"routine create failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def ops_center(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    resp = await c.get("/v1/ops/center")
    if resp.status_code != 200:
        raise CliError(f"ops failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def filter_report(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    c = client or _client()
    aggregate_resp = await c.get("/v1/filter/ledger/aggregate")
    if aggregate_resp.status_code != 200:
        raise CliError(
            f"filter report failed ({aggregate_resp.status_code}): {aggregate_resp.text[:500]}"
        )
    ledger_resp = await c.get("/v1/filter/ledger", params={"limit": 10})
    if ledger_resp.status_code != 200:
        raise CliError(
            f"filter report failed ({ledger_resp.status_code}): {ledger_resp.text[:500]}"
        )
    return {"aggregate": aggregate_resp.json(), "recent": ledger_resp.json()}


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
    owner_name: str | None = None,
    consent_tracks: list[str] | None = None,
    sample_paths: list[str | Path] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Guided first memories: capture N initial memories, then show their audit."""
    c = client or _client()
    identity: dict | None = None
    consents: list[dict] = []
    voice: dict | None = None
    if owner_name:
        status_resp = await c.get("/v1/identity/status")
        if status_resp.status_code == 200 and status_resp.json().get("owner_established"):
            codes_resp = await c.post("/v1/identity/recovery/codes")
            if codes_resp.status_code == 200:
                identity = codes_resp.json()
        else:
            identity = await identity_owner_create(owner_name, client=c)
    granted: set[str] = set()
    for track in consent_tracks or []:
        row = await consent_grant(track, client=c)
        granted.add(row["track"])
        consents.append(row)
    if sample_paths:
        if "voice_enrollment" not in granted:
            row = await consent_grant("voice_enrollment", client=c)
            granted.add("voice_enrollment")
            consents.append(row)
        voice = await voice_enroll(
            list(sample_paths),
            liveness="live",
            reason="onboarding",
            client=c,
        )
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
        # Queue mode writes memories asynchronously, so poll briefly for the
        # derived memory before showing its audit trail.
        for _ in range(15):
            search = await c.get("/v1/memories", params={"q": text, "limit": 1})
            if search.status_code == 200 and search.json().get("memories"):
                memory = search.json()["memories"][0]
                audit_resp = await c.get(f"/v1/audit/{memory['id']}")
                if audit_resp.status_code == 200:
                    audits.append(audit_resp.json())
                    break
            await asyncio.sleep(2)
    return {
        "events": events,
        "audits": audits,
        "identity": identity,
        "consents": consents,
        "voice": voice,
    }


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
    if cmd == "attach":
        async with _client(120.0) as client:
            result = await attach(
                args.file,
                source=args.source,
                event_type=args.event_type,
                privacy_level=args.privacy,
                device_id=args.device_id,
                client=client,
            )
        if result.get("queued"):
            print(
                f"EV is offline — attachment queued ({result['idempotency_key']}) "
                "for `ev sync`."
            )
            return 0
        attachment = result["attachment"]
        event = result["event"]
        print(
            f"attached {event['id']} -> {attachment['id']} "
            f"({attachment['filename']}, {attachment['size_bytes']} bytes, "
            f"sha256 {attachment['sha256'][:12]})"
        )
        return 0
    if cmd == "collect":
        from clients.collectors.agent import run_agent

        try:
            await run_agent(interval_seconds=args.interval, once=args.once)
        except SystemExit as exc:
            raise CliError(str(exc) or "collector configuration missing") from None
        return 0
    if cmd == "vision":
        from clients.cli import vision as vision_cli

        async with _client(120.0) as client:
            if args.vision_command == "list":
                rows = await vision_cli.list_perceptions(client, limit=args.limit)
                for row in rows:
                    labels = ", ".join(item["label"] for item in row.get("labels", []))
                    print(
                        f"{row['id']}  {row['summary'][:80]}"
                        f"  raw_sent={row['raw_sent']}"
                        + (f"  labels: {labels}" if labels else "")
                    )
                if not rows:
                    print("no perceptions recorded")
            elif args.vision_command == "pending":
                rows = await vision_cli.list_pending(client, limit=args.limit)
                for row in rows:
                    print(f"{row['id']}  {row['label']}  conf={row['confidence']}")
                if not rows:
                    print("no labels awaiting confirmation")
            elif args.vision_command == "confirm":
                confirmed = await vision_cli.confirm_recognition(
                    client,
                    args.recognition_id,
                    entity_type=args.type,
                )
                print(
                    f"confirmed {confirmed['label']} "
                    f"(source={confirmed['source']}, entity={confirmed['entity_id']})"
                )
            elif args.vision_command == "analyze":
                result = await vision_cli.analyze_attachment(
                    client,
                    args.attachment_id,
                    allow_raw=args.allow_raw,
                    prompt=args.prompt,
                )
                print(f"perception {result['id']}: {result['summary']}")
                if result.get("labels"):
                    print("labels: " + ", ".join(item["label"] for item in result["labels"]))
                print(f"raw_sent={result['raw_sent']} provider={result['provider']}")
        return 0
    if cmd == "ask":
        async with _client(120.0) as client:
            if args.no_stream:
                result = await ask(args.question, client=client)
                print(result["reply"])
                for item in result.get("provenance", []):
                    print(
                        f"  [source {item['memory_type']} score={item['score']}] "
                        f"{item['text'][:100]}"
                    )
            else:
                tokens: list[str] = []
                provenance: list[dict] = []

                def _delta(text: str) -> None:
                    tokens.append(text)
                    print(text, end="", flush=True)

                def _refined(text: str) -> None:
                    # Streaming refinement replaces the draft; show the final
                    # answer on its own line so scripts can consume it.
                    print("\n[refined] " + text)

                def _provenance(item: dict) -> None:
                    provenance.append(item)

                try:
                    await ask_stream(
                        args.question,
                        client=client,
                        on_delta=_delta,
                        on_refined=_refined,
                        on_provenance=_provenance,
                    )
                except KeyboardInterrupt:
                    print("\n[stream cancelled]", file=sys.stderr)
                    return 130
                print()
                if tokens:
                    print("".join(tokens), file=sys.stderr)
                for item in provenance:
                    print(
                        f"  [source {item.get('memory_type')} score={item.get('score')}] "
                        f"{str(item.get('text', ''))[:100]}"
                    )
        return 0
    if cmd == "voice":
        action = args.voice_command
        if action == "wake":
            async with _client() as client:
                result = await voice_wake(
                    device_id=args.device_id,
                    wake_word=args.wake_word,
                    priority=args.priority,
                    text_hint=args.text_hint,
                    client=client,
                )
            print(
                f"state={result['state']} owner_enrolled={result['owner_enrolled']} "
                f"session={result.get('session_id')}"
            )
            if result.get("challenge_nonce"):
                print(f"nonce: {result['challenge_nonce']}")
            if result.get("challenge_phrase"):
                print(f"challenge: {result['challenge_phrase']}")
            if result.get("message"):
                print(result["message"])
            return 0
        if action == "verify":
            async with _client(120.0) as client:
                result = await voice_session_verify(
                    args.session_id,
                    args.samples,
                    nonce=args.nonce,
                    phrase=args.phrase,
                    liveness=args.liveness,
                    live_score=args.live_score,
                    client=client,
                )
            print(
                f"verified={result['verified']} state={result['state']} "
                f"confidence={result['confidence']:.3f} reason={result['reason']}"
            )
            return 0
        if action == "listen":
            partials: list[dict] = []

            def _partial(item: dict) -> None:
                partials.append(item)
                marker = "stable" if item.get("stable") else "partial"
                print(f"  [{marker}] {item.get('text', '')}", flush=True)

            def _final(item: dict) -> None:
                print(
                    f"transcript: {item.get('text', '')} "
                    f"(conf={item.get('confidence', 0):.3f} "
                    f"provider={item.get('provider')})"
                )

            async with _client(120.0) as client:
                result = await voice_listen(
                    args.session_id,
                    text=args.text,
                    audio=args.audio,
                    conversation_id=args.conversation_id,
                    follow_up=args.follow_up,
                    client=client,
                    on_partial=_partial,
                    on_final=_final,
                )
            print(f"reply: {result.get('reply', '')}")
            tts = result.get("tts") or {}
            if tts.get("audio_ref"):
                print(f"audio: {tts['audio_ref']} ({tts.get('provider')})")
            if result.get("memory_deltas"):
                for delta in result["memory_deltas"]:
                    print(
                        f"  memory {delta['action']}: {delta['memory_type']} — "
                        f"{delta['text'][:100]}"
                    )
            return 0
        if action == "status":
            async with _client() as client:
                result = await voice_status(args.session_id, client=client)
            print(
                f"session={result.get('session_id')} state={result['state']} "
                f"owner_verified={result.get('owner_verified')} "
                f"follow_up_remaining={result.get('follow_up_remaining_seconds')}"
            )
            return 0
        if action == "end":
            async with _client() as client:
                result = await voice_end(args.session_id, client=client)
            print(
                f"ended session={result.get('session_id')} "
                f"reason={result.get('end_reason')}"
            )
            return 0
        raise CliError(f"unknown voice command: {action}")
    if cmd == "notify":
        if args.action == "test":
            async with _client() as client:
                row = await notify_send(
                    args.title,
                    args.body,
                    priority=args.priority,
                    tier=args.tier,
                    kind=args.kind,
                    source=args.source,
                    emergency=args.emergency,
                    client=client,
                )
            print(
                f"notification {row['id']} status={row['status']} "
                f"backend={row.get('backend') or 'none'}"
            )
            if row.get("reason"):
                print(f"reason: {row['reason']}")
            return 0
        if args.action == "history":
            async with _client() as client:
                rows = await notify_history(
                    status=args.status,
                    limit=args.limit,
                    client=client,
                )
            if not rows:
                print("no notifications")
                return 0
            for row in rows:
                print(
                    f"{row['queued_at']}  {row['status']:<10} "
                    f"tier={row['tier']}  {row['title']}  {row['id']}"
                )
            return 0
        if args.action == "status":
            async with _client() as client:
                report = await notify_status_report(client=client)
            print(
                f"backend={report['backend']} available={report['available']} "
                f"permission={report.get('permission') or 'n/a'}"
            )
            print(
                f"today: delivered={report['delivered_today']} "
                f"suppressed={report['suppressed_today']} "
                f"failed={report['failed_today']}"
            )
            if report.get("reason"):
                print(f"reason: {report['reason']}")
            return 0
        raise CliError(f"unknown notify command: {args.action}")
    if cmd == "model":
        action = args.model_command
        if action == "list":
            async with _client() as client:
                result = await model_list(client=client)
            print(f"provider: {result['provider']}")
            for model in result.get("models", []):
                print(f"  {model}")
            return 0
        if action == "pull":
            return model_pull(args.name)
        if action == "prune":
            return model_prune(all_files=args.all, dry_run=args.dry_run)
        if action == "stats":
            return model_stats()
        raise CliError(f"unknown model command: {action}")
    if cmd == "people":
        action = args.people_command
        if action == "enroll":
            async with _client(120.0) as client:
                result = await people_enroll(
                    args.name,
                    args.photos,
                    quality=args.quality,
                    confidence=args.confidence,
                    reason=args.reason,
                    client=client,
                )
            enrollment = result["enrollment"]
            print(
                f"enrolled {enrollment['person_name']} "
                f"(enrollment {enrollment['id']}, entity {enrollment['entity_id']}, "
                f"v{enrollment['version']}, {result['sample_count']} photos, "
                f"provider={result['provider']}, degraded={result['degraded']})"
            )
            return 0
        if action == "list":
            async with _client() as client:
                rows = await people_list(client=client)
            if not rows:
                print("no people enrolled")
                return 0
            for row in rows:
                state = "current" if row.get("is_current") else row.get("status")
                print(
                    f"{row['entity_id']}  {row['person_name']:<24} v{row['version']} "
                    f"{state:<8} {row['sample_count']} photos  {row['algorithm']}  "
                    f"{row['created_at']}"
                )
            return 0
        if action == "forget":
            async with _client() as client:
                manifest = await people_forget(args.entity_id, reason=args.reason, client=client)
            print(
                f"person erased: enrollments={manifest.get('face_enrollments_processed')} "
                f"samples={manifest.get('face_samples_deleted')} "
                f"sightings={manifest.get('recognition_logs_deleted')}"
            )
            return 0
        if action == "correct":
            async with _client() as client:
                row = await people_correct(
                    args.recognition_id,
                    args.label,
                    reason=args.reason,
                    client=client,
                )
            print(
                f"recognition {row['recognition_id']} corrected -> {row['label']} "
                f"(confirmed={row['confirmed']})"
            )
            return 0
        raise CliError(f"unknown people command: {action}")
    if cmd == "train":
        action = args.train_command
        if action == "dry-run":
            async with _client(120.0) as client:
                result = await train_dry_run(
                    args.corpus_version,
                    provider=args.provider,
                    base_model=args.base_model,
                    adapter_ref=args.adapter_ref,
                    client=client,
                )
            print(
                f"dry-run passed={result['passed']} provider={result['provider']} "
                f"corpus_v{result['corpus_version']}"
            )
            print(json.dumps(result.get("gates", {}), indent=2, default=str))
            return 0
        if action == "status":
            async with _client() as client:
                rows = await train_status(client=client)
            if not rows:
                print("no adapters")
                return 0
            for row in rows:
                print(
                    f"{row['id']}  {row['name']:<28} v{row['version']} "
                    f"{row['status']:<10} current={row['is_current']} "
                    f"provider={row['provider']}  {row['created_at']}"
                )
            return 0
        if action == "rollback":
            async with _client() as client:
                row = await train_rollback(
                    args.adapter_id,
                    reason=args.reason,
                    client=client,
                )
            print(f"rolled back {row['id']} -> status={row['status']} v{row['version']}")
            return 0
        raise CliError(f"unknown train command: {action}")
    if cmd == "eval":
        action = args.eval_command
        if action == "retrieval":
            report = await eval_retrieval(
                k=args.k,
                rerank=args.rerank,
                questions=args.questions,
                out=args.out,
                database_url=args.database_url,
            )
            if report.get("before_after"):
                before = report["before_after"]
                print(
                    f"retrieval eval: ndcg@10 {before['provider']['ndcg_at_10']} "
                    f"(delta {before['delta']['ndcg_at_10']}), "
                    f"MRR {before['provider']['mrr']}, "
                    f"top5 {before['provider']['top5_hit_rate']}"
                )
            return 0
        if action == "asr":
            report = await eval_asr(
                audio=args.audio,
                expected=args.expected,
                language=args.language,
            )
            if report.get("status") == "error":
                print(
                    f"asr eval error ({report['code']}): {report['message']}",
                    file=sys.stderr,
                )
                print(report["note"])
                return 1
            print(
                f"asr eval: provider={report['provider']} "
                f"degraded={report['degraded']} confidence={report['confidence']}"
            )
            print(f"transcript: {report['transcript']}")
            if report.get("expected"):
                print(f"exact match: {report['exact_match']}")
                print(f"wer: {report.get('wer')}")
            if report.get("note"):
                print(report["note"])
            return 0
        raise CliError(f"unknown eval command: {action}")
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
    if cmd == "consent":
        async with _client() as client:
            if args.action == "grant":
                row = await consent_grant(args.track, purpose=args.purpose, client=client)
                print(f"consent granted: {row['track']} ({row['id']})")
            elif args.action == "revoke":
                row = await consent_revoke(args.track, reason=args.reason, client=client)
                print(f"consent revoked: {row['track']} ({row['id']})")
            else:
                rows = await consent_list(client=client)
                if not rows:
                    print("no consents on record")
                for row in rows:
                    state_label = "active" if row.get("revoked_at") is None else "revoked"
                    print(f"{row['track']:<28} {state_label:<8} {row['granted_at']}")
        return 0
    if cmd == "voice-enroll":
        async with _client(120.0) as client:
            result = await voice_enroll(
                args.samples,
                liveness=args.liveness,
                live_score=args.live_score,
                reason=args.reason,
                client=client,
            )
        enrollment = result["enrollment"]
        print(
            f"enrolled {enrollment['id']} v{enrollment['version']} "
            f"samples {result['sample_count']} status {enrollment['status']}"
        )
        return 0
    if cmd == "voice-verify":
        async with _client(120.0) as client:
            result = await voice_verify(args.samples, client=client)
        print(
            f"accepted: {result['accepted']} · score {result['score']:.3f} "
            f"/ threshold {result['threshold']:.3f} · {result.get('reason', '')}"
        )
        return 0
    if cmd == "routines":
        async with _client(120.0) as client:
            if args.action == "run":
                run = await run_routine(args.routine_id, client=client)
                print(f"routine run {run['id']} status {run['status']}")
            elif args.action == "templates":
                templates = await list_routine_templates(client=client)
                if not templates:
                    print("no routine templates")
                for template in templates:
                    print(
                        f"{template.get('key', template.get('id', '?')):<28} "
                        f"{template.get('name', '')} — {template.get('description', '')}"
                    )
            elif args.action == "create":
                routine = await create_routine(
                    args.name,
                    args.action_type,
                    schedule=args.schedule,
                    kind=args.kind,
                    client=client,
                )
                print(f"created routine {routine['id']} ({routine['name']})")
            else:
                overview = await routines_overview(client=client)
                print(
                    f"routines {overview.get('routines_total', '?')} "
                    f"(enabled {overview.get('routines_enabled', '?')}) · "
                    f"runs 24h {overview.get('runs_last_24h', '?')} · "
                    f"awaiting approval {overview.get('awaiting_approval', '?')}"
                )
                routines = await list_routines(client=client)
                if not routines:
                    print("no routines configured")
                for routine in routines:
                    state = "enabled" if routine.get("enabled") else "disabled"
                    print(
                        f"{routine['id']}  {state:<8} {routine['kind']:<10} "
                        f"{routine['name']}  {routine.get('schedule') or ''}  "
                        f"{routine['action_type']}"
                    )
        return 0
    if cmd == "ops":
        async with _client() as client:
            report = await ops_center(client=client)
        focus = report.get("focus") or {}
        print(f"focus: {focus.get('label') or '(none)'}")
        print(f"alerts: {len(report.get('alerts', []))} · "
              f"open decisions: {len(report.get('open_decisions', []))} · "
              f"patterns: {len(report.get('patterns', []))}")
        for action in report.get("next_actions", []):
            print(f"  next: {action}")
        return 0
    if cmd == "filter-report":
        async with _client() as client:
            report = await filter_report(client=client)
        aggregate = report["aggregate"]
        print(json.dumps(aggregate, indent=2, default=str))
        recent = report["recent"]
        if not recent:
            print("no recent filter evaluations")
        for entry in recent:
            print(
                f"{entry.get('evaluated_at', '')}  {entry.get('decision', '')}  "
                f"{entry.get('blocked', False)}  {entry.get('reason', '')[:100]}"
            )
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
    if cmd == "workbench":
        info = workbench_info()
        print(f"workbench: {info['url']}")
        expected = "yes" if info["loopback_auto_connect"] else "no"
        print(f"loopback auto-connect expected: {expected}")
        print("ops console: {url}/ops".format(url=info["url"]))
        return 0
    if cmd == "present":
        async with _client() as client:
            result = await present(
                args.title,
                args.body,
                kind=args.kind,
                size=getattr(args, "size", None),
                time_type=getattr(args, "time_type", None),
                placement=getattr(args, "placement", None),
                ttl_ms=getattr(args, "ttl_ms", None),
                lookout=bool(getattr(args, "lookout", False)),
                auto=bool(getattr(args, "auto", False)),
                client=client,
            )
        state = "opened" if result.get("opened") else "not opened"
        print(f"EVIE overlay {state} via {result.get('via') or result.get('reason')}")
        if result.get("next_step"):
            print(result["next_step"])
        return 0 if result.get("opened") else 2
    if cmd == "checkup":
        async with _client(120.0) as client:
            report = await checkup(client=client)
        print(json.dumps(report, indent=2, default=str))
        return 0
    if cmd == "protocols":
        async with _client() as client:
            sheet = await list_protocols(client=client)
        for item in sheet.get("protocols") or []:
            print(f"{item.get('status', '?'):12} {item.get('title')} — {item.get('detail', '')}")
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
        async with _client(120.0) as client:
            result = await onboarding(
                texts,
                owner_name=args.owner,
                consent_tracks=args.consent,
                sample_paths=args.samples,
                client=client,
            )
        if result.get("identity"):
            identity = result["identity"]
            print(f"owner {identity.get('owner_id', '?')} ready; one-time recovery codes:")
            for code in identity.get("recovery_codes", []):
                print(f"  {code.get('label') or 'code'}: {code['code']} (expires {code.get('expires_at')})")
        for row in result.get("consents", []):
            print(f"consent granted: {row['track']}")
        if result.get("voice"):
            enrollment = result["voice"]["enrollment"]
            print(
                f"voice enrolled v{enrollment['version']} "
                f"({result['voice']['sample_count']} samples)"
            )
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
            if record.get("kind") == "attachment":
                print(
                    f"{record['queued_at']}  attachment  {record['idempotency_key']}  "
                    f"{record.get('file_path', '')}"
                )
            else:
                print(
                    f"{record['queued_at']}  capture  {record['idempotency_key']}  "
                    f"{payload.get('text', '')[:120]}"
                )
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
    if cmd == "identity":
        action = args.identity_command
        if action == "status":
            async with _client() as client:
                status = await identity_status(client=client)
            print(f"owner: {'established' if status['owner_established'] else 'NOT ESTABLISHED'}")
            print(f"trust: {status['trust_level']}  actor: {status['actor']}")
            print(f"devices: {status['devices_active']}  passkeys: {status['passkeys_active']}")
            print(f"recovery codes remaining: {status['recovery_codes_remaining']}")
            print(f"recovery locked: {status['recovery_locked']}")
            return 0
        if action == "owner":
            async with _client() as client:
                result = await identity_owner_create(args.name, client=client)
            print(f"owner {result['owner_id']} created: {result['display_name']}")
            print("one-time recovery codes (store offline, never in EV):")
            for item in result["recovery_codes"]:
                print(f"  {item['label']}: {item['code']}  (expires {item['expires_at']})")
            return 0
        if action == "passkey":
            passkey_action = args.identity_passkey_command
            if passkey_action == "add":
                async with _client() as client:
                    result = await identity_passkey_add(
                        args.credential_id,
                        args.name,
                        device_id=args.device_id,
                        client=client,
                    )
                print(f"passkey {result['passkey']['id']} registered: {result['passkey']['name']}")
                return 0
            if passkey_action == "list":
                async with _client() as client:
                    rows = await identity_passkey_list(client=client)
                if not rows:
                    print("no passkeys registered")
                    return 0
                for row in rows:
                    print(
                        f"{row['id']}  {row['name']}  "
                        f"device={row.get('device_id') or 'any'}  "
                        f"created={row['created_at']}"
                    )
                return 0
            if passkey_action == "remove":
                async with _client() as client:
                    result = await identity_passkey_remove(args.passkey_id, client=client)
                print(f"passkey {result['id']} revoked ({result['revoked_reason']})")
                return 0
            raise CliError(f"unknown identity passkey command: {passkey_action}")
        if action == "recovery":
            result = await identity_recovery_redeem(
                args.code,
                args.device_name,
                capabilities=args.capability,
            )
            print(f"recovery redeemed -> owner {result['owner_id']}")
            print(f"new device {result['device']['id']} ({result['device']['trust_level']})")
            print(f"device token: {result['token']}")
            print("prior devices were revoked; store this token in the keychain.")
            return 0
        if action == "verify":
            async with _client() as client:
                result = await identity_reverification_issue(
                    args.purpose,
                    session_id=args.session_id,
                    client=client,
                )
            print(result["token"])
            print(
                f"purpose={result['purpose']} expires={result['expires_at']} "
                "(single-use; pass as X-EV-Reverify)",
                file=sys.stderr,
            )
            return 0
        raise CliError(f"unknown identity command: {action}")
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

    p_attach = sub.add_parser("attach", help="capture a file/share as an attachment event")
    p_attach.add_argument("file", help="path to the file to capture")
    p_attach.add_argument("--source", default="attachment", help="event source")
    p_attach.add_argument("--event-type", default="file", help="event type")
    p_attach.add_argument(
        "--privacy",
        default="normal",
        choices=["private", "normal", "sensitive", "never_send_to_model"],
    )
    p_attach.add_argument("--device-id", default=None, help="device identifier")

    p_collect = sub.add_parser(
        "collect",
        help="run privacy-preserving perception collectors (screen/audio/location)",
    )
    p_collect.add_argument("--once", action="store_true", help="collect one sample and exit")
    p_collect.add_argument("--interval", type=int, default=30, help="loop interval seconds")

    p_vision = sub.add_parser("vision", help="perception audit: list, review, confirm labels")
    vision_sub = p_vision.add_subparsers(dest="vision_command", required=True)
    p_vision_list = vision_sub.add_parser("list", help="list perception records")
    p_vision_list.add_argument("--limit", type=int, default=50)
    p_vision_pending = vision_sub.add_parser("pending", help="labels awaiting user confirmation")
    p_vision_pending.add_argument("--limit", type=int, default=50)
    p_vision_confirm = vision_sub.add_parser("confirm", help="confirm a model-suggested label")
    p_vision_confirm.add_argument("recognition_id")
    p_vision_confirm.add_argument(
        "--type",
        default="thing",
        choices=["person", "place", "project", "topic", "thing"],
    )
    p_vision_analyze = vision_sub.add_parser("analyze", help="analyze an attachment (explicit permission)")
    p_vision_analyze.add_argument("attachment_id")
    p_vision_analyze.add_argument(
        "--allow-raw",
        action="store_true",
        help="permit raw media to a vision-capable provider",
    )
    p_vision_analyze.add_argument("--prompt", default=None)

    p_ask = sub.add_parser("ask", help="ask EV a question (streams tokens by default)")
    sub.add_parser("protocols", help="list unlocked and refused protocols")
    p_ask.add_argument("question")
    p_ask.add_argument(
        "--no-stream",
        action="store_true",
        help="use the buffered /v1/chat response instead of SSE",
    )

    p_voice = sub.add_parser("voice", help="voice session: wake, verify, listen, end")
    voice_sub = p_voice.add_subparsers(dest="voice_command", required=True)
    p_voice_wake = voice_sub.add_parser("wake", help="wake a voice session")
    p_voice_wake.add_argument("--device-id", default="cli", help="device identifier")
    p_voice_wake.add_argument("--wake-word", default="evie")
    p_voice_wake.add_argument("--priority", type=float, default=0.5)
    p_voice_wake.add_argument("--text-hint", default=None, help="dev-double wake hint")
    p_voice_verify = voice_sub.add_parser("verify", help="verify the owner voiceprint")
    p_voice_verify.add_argument("session_id")
    p_voice_verify.add_argument("samples", nargs="+", help="audio sample files")
    p_voice_verify.add_argument("--nonce", default=None)
    p_voice_verify.add_argument("--phrase", default=None)
    p_voice_verify.add_argument(
        "--liveness",
        default="live",
        choices=["live", "replay", "synthetic", "converted"],
    )
    p_voice_verify.add_argument("--live-score", type=float, default=0.9)
    p_voice_listen = voice_sub.add_parser(
        "listen",
        help="stream an utterance (partial ASR -> transcript -> reply)",
    )
    p_voice_listen.add_argument("session_id")
    p_voice_listen.add_argument("--text", default=None, help="text hint (dev double)")
    p_voice_listen.add_argument("--audio", default=None, help="audio file (base64 upload)")
    p_voice_listen.add_argument("--conversation-id", default=None)
    p_voice_listen.add_argument("--follow-up", action="store_true")
    p_voice_status = voice_sub.add_parser("status", help="show session state")
    p_voice_status.add_argument("session_id")
    p_voice_end = voice_sub.add_parser("end", help="end a voice session")
    p_voice_end.add_argument("session_id")

    p_notify = sub.add_parser("notify", help="notification delivery: test, history, status")
    notify_sub = p_notify.add_subparsers(dest="action", required=True)
    p_notify_test = notify_sub.add_parser("test", help="dispatch one test notification")
    p_notify_test.add_argument("--title", required=True)
    p_notify_test.add_argument("--body", required=True)
    p_notify_test.add_argument("--priority", type=float, default=0.5)
    p_notify_test.add_argument(
        "--tier",
        default="useful",
        choices=["urgent", "useful", "background", "notify", "notify_card"],
    )
    p_notify_test.add_argument("--kind", default="manual")
    p_notify_test.add_argument("--source", default=None)
    p_notify_test.add_argument("--emergency", action="store_true")
    p_notify_history = notify_sub.add_parser("history", help="delivery receipts")
    p_notify_history.add_argument(
        "--status",
        default=None,
        choices=["attempted", "delivered", "failed", "suppressed"],
    )
    p_notify_history.add_argument("--limit", type=int, default=50)
    notify_sub.add_parser("status", help="backend + today's receipt counts")

    p_model = sub.add_parser("model", help="model registry and cache operations")
    model_sub = p_model.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("list", help="list gateway provider models (API)")
    p_model_pull = model_sub.add_parser(
        "pull",
        help="download + checksum-verify a model (local Agent 2 cache)",
    )
    p_model_pull.add_argument("name")
    p_model_prune = model_sub.add_parser(
        "prune",
        help="evict least-recently-used cached weights (local)",
    )
    p_model_prune.add_argument("--all", action="store_true")
    p_model_prune.add_argument("--dry-run", action="store_true")
    model_sub.add_parser("stats", help="print local arbiter stats (ceiling, resident MB, backend)")

    p_people = sub.add_parser("people", help="consented person roster")
    people_sub = p_people.add_subparsers(dest="people_command", required=True)
    p_people_enroll = people_sub.add_parser(
        "enroll",
        help="enroll a person from >=5 aligned photo crops",
    )
    p_people_enroll.add_argument("name")
    p_people_enroll.add_argument("photos", nargs="+", help="photo files")
    p_people_enroll.add_argument("--quality", type=float, default=0.99)
    p_people_enroll.add_argument("--confidence", type=float, default=0.99)
    p_people_enroll.add_argument("--reason", default=None)
    people_sub.add_parser("list", help="list face enrollments")
    p_people_forget = people_sub.add_parser("forget", help="erase a person permanently")
    p_people_forget.add_argument("entity_id")
    p_people_forget.add_argument("--reason", default="user requested person deletion")
    p_people_correct = people_sub.add_parser(
        "correct",
        help="correct a face recognition label (recognition id from Agent 7)",
    )
    p_people_correct.add_argument("recognition_id")
    p_people_correct.add_argument("label")
    p_people_correct.add_argument("--reason", default="cli correction")

    p_train = sub.add_parser("train", help="fine-tuning adapter lifecycle")
    train_sub = p_train.add_subparsers(dest="train_command", required=True)
    p_train_dry = train_sub.add_parser("dry-run", help="validate dataset + gates")
    p_train_dry.add_argument("--corpus-version", type=int, required=True)
    p_train_dry.add_argument("--provider", default="local-lora")
    p_train_dry.add_argument("--base-model", default=None)
    p_train_dry.add_argument("--adapter-ref", default=None)
    train_sub.add_parser("status", help="list adapters")
    p_train_rollback = train_sub.add_parser("rollback", help="roll back an adapter")
    p_train_rollback.add_argument("adapter_id")
    p_train_rollback.add_argument("--reason", default="rollback adapter")

    p_eval = sub.add_parser("eval", help="run local eval harnesses")
    eval_sub = p_eval.add_subparsers(dest="eval_command", required=True)
    p_eval_retrieval = eval_sub.add_parser(
        "retrieval",
        help="run Agent 8's retrieval eval (synthetic corpus by default)",
    )
    p_eval_retrieval.add_argument("--k", type=int, default=10)
    p_eval_retrieval.add_argument("--rerank", action="store_true")
    p_eval_retrieval.add_argument("--questions", default=None)
    p_eval_retrieval.add_argument("--out", default=None)
    p_eval_retrieval.add_argument("--database-url", default=None)
    p_eval_asr = eval_sub.add_parser(
        "asr",
        help="self-probe the configured ASR factory (transcript/confidence/degraded)",
    )
    p_eval_asr.add_argument("--audio", default=None, help="audio file for real ASR")
    p_eval_asr.add_argument("--expected", default=None, help="expected transcript")
    p_eval_asr.add_argument("--language", default="en")

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

    p_consent = sub.add_parser("consent", help="training consent lifecycle")
    p_consent_sub = p_consent.add_subparsers(dest="action", required=True)
    p_consent_grant = p_consent_sub.add_parser("grant", help="grant a training track")
    p_consent_grant.add_argument("track", help="voice_enrollment, training_corpus, ...")
    p_consent_grant.add_argument("--purpose", default="personalize EV to the owner")
    p_consent_revoke = p_consent_sub.add_parser("revoke", help="revoke a training track")
    p_consent_revoke.add_argument("track")
    p_consent_revoke.add_argument("--reason", default="user revoked")
    p_consent_sub.add_parser("list", help="list consents")

    p_voice_enroll = sub.add_parser(
        "voice-enroll",
        help="enroll the owner voiceprint from audio sample files (≥5)",
    )
    p_voice_enroll.add_argument("samples", nargs="+", help="audio sample files")
    p_voice_enroll.add_argument(
        "--liveness",
        default=None,
        choices=["live", "replay", "synthetic", "converted"],
        help="liveness proof label (default: live score path)",
    )
    p_voice_enroll.add_argument("--live-score", type=float, default=None, help="0..1")
    p_voice_enroll.add_argument("--reason", default=None)

    p_voice_verify = sub.add_parser(
        "voice-verify",
        help="verify the owner voiceprint from audio sample files",
    )
    p_voice_verify.add_argument("samples", nargs="+", help="audio sample files")

    p_routines = sub.add_parser("routines", help="automation routines")
    p_routines_sub = p_routines.add_subparsers(dest="action", required=True)
    p_routines_sub.add_parser("list", help="list routines + overview")
    p_routines_sub.add_parser("templates", help="list routine templates")
    p_routines_run = p_routines_sub.add_parser("run", help="run a routine now")
    p_routines_run.add_argument("routine_id")
    p_routines_create = p_routines_sub.add_parser("create", help="create a routine")
    p_routines_create.add_argument("name")
    p_routines_create.add_argument("--action-type", required=True)
    p_routines_create.add_argument("--schedule", default=None)
    p_routines_create.add_argument("--kind", default="scheduled", choices=["scheduled", "trigger"])

    sub.add_parser("ops", help="ops center snapshot")
    sub.add_parser("filter-report", help="intelligence filter ledger report")

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
    sub.add_parser(
        "workbench",
        help="print the workbench URL and whether loopback auto-connect applies",
    )
    p_present = sub.add_parser(
        "present",
        help="open EVIE's native HUD overlay on this Mac",
    )
    p_present.add_argument("title")
    p_present.add_argument("body")
    p_present.add_argument("--kind", default="card")
    p_present.add_argument("--size", default=None)
    p_present.add_argument("--time-type", dest="time_type", default=None)
    p_present.add_argument("--placement", default=None)
    p_present.add_argument("--ttl-ms", dest="ttl_ms", type=int, default=None)
    p_present.add_argument("--lookout", action="store_true")
    p_present.add_argument("--auto", action="store_true")
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
    p_onboarding.add_argument("--owner", default=None, help="display name to establish the owner")
    p_onboarding.add_argument(
        "--consent",
        action="append",
        default=[],
        help="training track to grant (repeatable: voice_enrollment, training_corpus, ...)",
    )
    p_onboarding.add_argument(
        "--samples",
        nargs="+",
        default=None,
        help="audio sample files for voice enrollment (needs 5)",
    )

    sub.add_parser("queue", help="list queued offline captures")
    sub.add_parser("sync", help="send queued offline captures")

    p_identity = sub.add_parser("identity", help="identity & trust lifecycle")
    id_sub = p_identity.add_subparsers(dest="identity_command", required=True)
    id_sub.add_parser("status", help="show owner binding and trust state")
    p_owner = id_sub.add_parser("owner", help="establish the owner identity (master key)")
    p_owner.add_argument("--name", default="Owner", help="display name")
    p_passkey = id_sub.add_parser("passkey", help="manage passkeys")
    pk_sub = p_passkey.add_subparsers(dest="identity_passkey_command", required=True)
    p_pk_add = pk_sub.add_parser("add", help="register a WebAuthn credential id")
    p_pk_add.add_argument("--credential-id", required=True, help="WebAuthn credential id")
    p_pk_add.add_argument("--name", required=True, help="human label")
    p_pk_add.add_argument("--device-id", default=None, help="bound device id")
    pk_sub.add_parser("list", help="list registered passkeys")
    p_pk_remove = pk_sub.add_parser("remove", help="revoke a passkey (master key)")
    p_pk_remove.add_argument("passkey_id", help="passkey id to revoke")
    p_recovery = id_sub.add_parser(
        "recovery",
        help="redeem a one-time recovery code (no API key required)",
    )
    p_recovery.add_argument("--code", required=True, help="recovery code")
    p_recovery.add_argument("--device-name", required=True, help="new device name")
    p_recovery.add_argument(
        "--capability",
        action="append",
        default=[],
        help="device capability (repeatable)",
    )
    p_verify = id_sub.add_parser(
        "verify",
        help="issue a single-use re-verification proof for a sensitive action",
    )
    p_verify.add_argument(
        "--purpose",
        required=True,
        choices=[
            "integration.action",
            "memory.delete",
            "runtime.action",
            "voice.revoke",
            "voice.delete",
            "recovery.rotate",
            "voice.sensitive_action",
        ],
    )
    p_verify.add_argument("--session-id", default=None, help="bound voice session id")
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
