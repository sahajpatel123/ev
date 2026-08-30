"""End-to-end tests for the EV CLI client and offline capture sync."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment
from clients.cli import (
    QUEUE_FILENAME,
    _iter_sse,
    ask_stream,
    attach,
    audit,
    build_parser,
    capture,
    card,
    consent_grant,
    consent_list,
    consent_revoke,
    correct,
    enqueue_attachment,
    enqueue_capture,
    eval_asr,
    export_bundle,
    filter_report,
    forget,
    identity_owner_create,
    identity_passkey_add,
    identity_passkey_list,
    identity_passkey_remove,
    identity_recovery_redeem,
    identity_reverification_issue,
    identity_status,
    import_bundle_file,
    list_queue,
    list_routine_templates,
    list_routines,
    memories,
    model_list,
    notify_history,
    notify_send,
    onboarding,
    ops_center,
    people_enroll,
    people_forget,
    people_list,
    restore,
    routines_overview,
    sync_captures,
    timeline,
    train_dry_run,
    train_status,
    voice_enroll,
    voice_listen,
    voice_session_verify,
    voice_verify,
    voice_wake,
    word_error_rate,
    workbench_info,
)


class OfflineTransport(httpx.AsyncBaseTransport):
    """Transport that simulates a lost connection."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)


def offline_client() -> AsyncClient:
    return AsyncClient(
        transport=OfflineTransport(),
        base_url="http://offline",
        headers={"Authorization": "Bearer test-key"},
    )


async def test_cli_capture_timeline_memories_audit_roundtrip(
    client: AsyncClient, tmp_path: Path
) -> None:
    result = await capture(
        "Remember: I prefer fixed-term contracts for client work now.",
        client=client,
        queue=tmp_path / "queue",
    )
    event = result["event"]
    assert event["source"] == "cli"
    assert event["event_type"] == "note"

    data = await timeline(client=client, limit=10)
    assert any(e["id"] == event["id"] for e in data["events"])

    data = await memories(client=client, q="fixed-term contracts", limit=10)
    assert data["total"] >= 1
    memory = data["memories"][0]

    trail = await audit(memory["id"], client=client)
    assert trail["memory"]["id"] == memory["id"]
    assert any(e["id"] == event["id"] for e in trail["source_events"])
    assert trail["versions"][0]["version"] == memory["version"]


async def test_cli_memory_correct_forget_restore(client: AsyncClient, tmp_path: Path) -> None:
    await capture("The enclosure needs a chamfered edge for the new gasket.", client=client)
    data = await memories(client=client, q="chamfered edge", limit=10)
    memory = data["memories"][0]

    corrected = await correct(
        memory["id"],
        "The enclosure needs a rounded edge for the new gasket.",
        reason="geometry fix",
        client=client,
    )
    assert corrected["version"] == memory["version"] + 1
    assert "rounded edge" in corrected["text"]
    assert corrected["is_current"] is True

    forgotten = await forget(corrected["id"], reason="test", client=client)
    assert forgotten["is_current"] is False
    assert forgotten["valid_until"] is not None

    restored = await restore(corrected["id"], client=client)
    assert restored["is_current"] is True
    assert restored["valid_until"] is None


async def test_idempotency_key_deduplicates_events(client: AsyncClient) -> None:
    payload = {
        "source": "cli",
        "event_type": "note",
        "text": "offline replay must not duplicate",
    }
    headers = {"Idempotency-Key": "cli-test-replay-1"}
    first = await client.post("/v1/events", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    event_id = first.json()["event"]["id"]

    second = await client.post("/v1/events", json=payload, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["event"]["id"] == event_id

    data = await timeline(client=client, limit=100)
    assert sum(1 for e in data["events"] if e["id"] == event_id) == 1


async def test_offline_capture_queues_and_syncs(client: AsyncClient, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    async with offline_client() as offline:
        result = await capture(
            "Captured while disconnected.",
            client=offline,
            queue=queue,
        )
    assert result["queued"] is True
    assert (queue / QUEUE_FILENAME).exists()
    records = list_queue(queue)
    assert len(records) == 1
    assert records[0]["payload"]["text"] == "Captured while disconnected."

    summary = await sync_captures(client, queue)
    assert summary["synced"] == 1
    assert summary["dropped"] == 0
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    data = await timeline(client=client, limit=100)
    texts = [
        (e.get("content") or {}).get("text", "")
        for e in data["events"]
        if e["source"] == "cli"
    ]
    assert "Captured while disconnected." in texts


async def test_sync_drops_duplicate_replays(client: AsyncClient, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    payload = {"source": "cli", "event_type": "note", "text": "idempotent replay"}
    enqueue_capture(payload, "cli-replay-key", queue)

    summary = await sync_captures(client, queue)
    assert summary["synced"] == 1

    enqueue_capture(payload, "cli-replay-key", queue)
    summary = await sync_captures(client, queue)
    assert summary["synced"] == 0
    assert summary["dropped"] == 1
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    data = await timeline(client=client, limit=100)
    texts = [
        (e.get("content") or {}).get("text", "")
        for e in data["events"]
        if e["source"] == "cli"
    ]
    assert texts.count("idempotent replay") == 1


async def test_hud_card_schema(client: AsyncClient, tmp_path: Path) -> None:
    hud = await card(client=client)
    assert hud["schema_version"] == "ev.hud.card.v1"
    assert hud["title"]
    assert hud["body"]
    assert "priority" in hud


async def test_sync_quarantines_invalid_payload(client: AsyncClient, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    enqueue_capture(
        {"source": "cli"},  # missing event_type -> 422 validation
        "cli-invalid-key",
        queue,
    )
    summary = await sync_captures(client, queue)
    assert summary["synced"] == 0
    assert summary["quarantined"] == 1
    assert summary["remaining"] == 0
    assert list_queue(queue) == []
    quarantine = queue / "quarantine.jsonl"
    assert quarantine.exists()
    entries = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert entries[0]["idempotency_key"] == "cli-invalid-key"


async def test_cli_import_bundle_merge_roundtrip(
    client: AsyncClient, tmp_path: Path
) -> None:
    await capture("Import me later.", client=client)
    bundle = await export_bundle(client=client)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, default=str), encoding="utf-8")

    summary = await import_bundle_file(path, mode="merge", client=client)
    assert summary["completed_at"]
    assert summary["events_total"] >= 1
    # Same DB: every event already exists, so the merge dedupes by content hash.
    assert summary["events_imported"] == 0
    assert summary["events_skipped"] >= 1


async def test_cli_onboarding_creates_first_memories_and_audit(
    client: AsyncClient,
) -> None:
    result = await onboarding(
        [
            "I prefer fixed-term contracts for client work.",
            "Goal: ship EV this month.",
            "Sam likes local AI tools.",
        ],
        client=client,
    )
    assert len(result["events"]) == 3
    assert all(event["source"] == "onboarding" for event in result["events"])
    assert result["audits"]
    first = result["audits"][0]
    assert first["memory"]["id"]
    assert first["source_events"]
    assert first["versions"]


async def test_cli_onboarding_full_flow(client: AsyncClient) -> None:
    result = await onboarding(
        ["Goal: ship EV this month."],
        owner_name="E2E Owner",
        consent_tracks=["voice_enrollment", "training_corpus"],
        client=client,
    )
    assert result["identity"]
    assert result["identity"]["recovery_codes"]
    tracks = {row["track"] for row in result["consents"]}
    assert {"voice_enrollment", "training_corpus"} <= tracks
    assert result["events"]
    assert result["audits"]


async def test_cli_attach_file_capture_roundtrip(
    client: AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "note.txt"
    payload = b"Attachment capture integrity check for EV."
    path.write_bytes(payload)

    result = await attach(path, client=client)
    attachment = result["attachment"]
    event = result["event"]

    assert attachment["filename"] == "note.txt"
    assert attachment["size_bytes"] == len(payload)
    assert event["source"] == "attachment"
    assert event["event_type"] == "file"
    assert (event.get("content") or {}).get("filename") == "note.txt"

    resp = await client.get(f"/v1/attachments/{attachment['id']}")
    assert resp.status_code == 200
    assert resp.content == payload


async def test_offline_attachment_queues_and_syncs(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    queue = tmp_path / "queue"
    file_path = tmp_path / "offline.txt"
    payload = b"offline attachment bytes"
    file_path.write_bytes(payload)

    async with offline_client() as offline:
        result = await attach(file_path, client=offline, queue=queue)
    assert result["queued"] is True
    records = list_queue(queue)
    assert len(records) == 1
    assert records[0]["kind"] == "attachment"
    assert records[0]["file_path"] == str(file_path)

    summary = await sync_captures(client, queue)
    assert summary["synced"] == 1
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    data = await timeline(client=client, limit=100)
    events = [e for e in data["events"] if e["source"] == "attachment"]
    assert events and (events[0]["content"] or {}).get("filename") == "offline.txt"

    rows = (await db_session.execute(select(Attachment))).scalars().all()
    assert len(rows) == 1
    resp = await client.get(f"/v1/attachments/{rows[0].id}")
    assert resp.status_code == 200
    assert resp.content == payload


async def test_sync_quarantines_attachment_with_missing_file(
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    queue = tmp_path / "queue"
    missing = tmp_path / "gone.txt"
    enqueue_attachment(
        {
            "source": "attachment",
            "event_type": "file",
            "privacy_level": "normal",
            "metadata": "{}",
        },
        missing,
        "cli-missing-file",
        queue,
    )

    summary = await sync_captures(client, queue)
    assert summary["quarantined"] == 1
    assert summary["synced"] == 0
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    quarantine = queue / "quarantine.jsonl"
    assert quarantine.exists()
    entries = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert "file missing" in entries[0]["reason"]


async def test_cli_consent_lifecycle(client: AsyncClient) -> None:
    granted = await consent_grant("voice_enrollment", client=client)
    assert granted["track"] == "voice_enrollment"
    assert granted["revoked_at"] is None

    rows = await consent_list(client=client)
    assert any(row["track"] == "voice_enrollment" and row["revoked_at"] is None for row in rows)

    revoked = await consent_revoke("voice_enrollment", reason="cli test", client=client)
    assert revoked["revoked_at"] is not None


async def test_cli_voice_enroll_with_liveness(client: AsyncClient, tmp_path: Path) -> None:
    await consent_grant("voice_enrollment", client=client)
    samples: list[str | Path] = []
    for index in range(5):
        path = tmp_path / f"sample-{index}.wav"
        path.write_bytes(b"voice-sample-" + str(index).encode() * 20)
        samples.append(path)

    result = await voice_enroll(
        samples,
        liveness="live",
        reason="cli test enrollment",
        client=client,
    )
    enrollment = result["enrollment"]
    assert result["sample_count"] == 5
    assert enrollment["status"] == "active"
    assert enrollment["version"] == 1


async def test_cli_voice_enroll_then_verify(client: AsyncClient, tmp_path: Path) -> None:
    await consent_grant("voice_enrollment", client=client)
    samples: list[str | Path] = []
    sample_bytes = b"voice-sample-" + b"x" * 512
    for index in range(5):
        path = tmp_path / f"verify-{index}.wav"
        path.write_bytes(sample_bytes)
        samples.append(path)

    enrolled = await voice_enroll(samples, liveness="live", client=client)
    assert enrolled["sample_count"] == 5

    verified = await voice_verify(samples, client=client)
    assert verified["accepted"] is True
    assert verified["score"] >= verified["threshold"]


async def test_cli_routines_ops_filter_report(client: AsyncClient) -> None:
    overview = await routines_overview(client=client)
    assert "routines_total" in overview
    assert "routines_enabled" in overview
    assert isinstance(await list_routines(client=client), list)
    assert isinstance(await list_routine_templates(client=client), list)

    report = await ops_center(client=client)
    assert "next_actions" in report

    filter_data = await filter_report(client=client)
    assert "aggregate" in filter_data
    assert "recent" in filter_data


async def test_cli_identity_owner_passkey_lifecycle(client: AsyncClient) -> None:
    status = await identity_status(client=client)
    assert status["owner_established"] is False

    owner = await identity_owner_create("Sahaj", client=client)
    assert owner["owner_id"]
    assert len(owner["recovery_codes"]) == 8

    status = await identity_status(client=client)
    assert status["owner_established"] is True
    assert status["trust_level"] == "master"

    registered = await identity_passkey_add(
        "cli-credential-id-0001",
        "cli key",
        client=client,
    )
    passkey_id = registered["passkey"]["id"]
    rows = await identity_passkey_list(client=client)
    assert len(rows) == 1

    revoked = await identity_passkey_remove(passkey_id, client=client)
    assert revoked["revoked_at"] is not None
    assert await identity_passkey_list(client=client) == []


async def test_cli_identity_verify_and_recovery_redeem(client: AsyncClient) -> None:
    owner = await identity_owner_create("Sahaj", client=client)
    proof = await identity_reverification_issue("memory.delete", client=client)
    assert proof["token"]
    assert proof["purpose"] == "memory.delete"

    # Recovery redeem works through the same API (real CLI path is unauthenticated).
    code = owner["recovery_codes"][0]["code"]
    redeemed = await identity_recovery_redeem(code, "new-phone", client=client)
    assert redeemed["device"]["trust_level"] == "owner"
    assert redeemed["token"]


async def test_cli_ask_stream_renders_tokens_and_provenance(
    client: AsyncClient,
) -> None:
    deltas: list[str] = []
    refined: list[str] = []
    provenances: list[dict] = []
    done = await ask_stream(
        "What do I prefer for client work?",
        client=client,
        on_delta=deltas.append,
        on_refined=refined.append,
        on_provenance=provenances.append,
    )
    assert deltas or refined
    assert done.get("conversation_id")
    assert provenances


async def test_cli_sse_parser_handles_partial_asr_events() -> None:
    lines = [
        "event: partial",
        'data: {"text": "Remind", "provider": "parakeet-eou-120m", "sequence": 1, "stable": false}',
        "",
        "event: partial",
        'data: {"text": "Remind me", "provider": "parakeet-eou-120m", "sequence": 2, "stable": true}',
        "",
        "event: done",
        'data: {"session_id": "s1"}',
        "",
    ]

    class FakeResponse:
        async def aiter_lines(self):
            for line in lines:
                yield line

    events = [(name, data) async for name, data in _iter_sse(FakeResponse())]
    assert events[0][0] == "partial"
    assert events[0][1]["text"] == "Remind"
    assert events[1][1]["stable"] is True
    assert events[2] == ("done", {"session_id": "s1"})


def _sse_bytes(events: list[tuple[str, dict]]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n"
        for name, payload in events
    ).encode()


async def test_cli_ask_stream_dispatches_events_via_mock_transport() -> None:
    body = _sse_bytes(
        [
            ("delta", {"text": "Hel", "final": False}),
            ("delta", {"text": "lo", "final": False}),
            ("provenance", {"memory_id": "m1", "text": "source", "memory_type": "fact"}),
            ("refined", {"text": "Hello", "replaces": True}),
            ("done", {"conversation_id": "c1", "model": "mock"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat"
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )

    deltas: list[str] = []
    refined: list[str] = []
    provenances: list[dict] = []
    async with AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mock",
    ) as client:
        done = await ask_stream(
            "hello",
            client=client,
            on_delta=deltas.append,
            on_refined=refined.append,
            on_provenance=provenances.append,
        )
    assert "".join(deltas) == "Hello"
    assert refined == ["Hello"]
    assert provenances[0]["memory_type"] == "fact"
    assert done == {"conversation_id": "c1", "model": "mock"}


async def test_cli_voice_listen_dispatches_partial_events_via_mock_transport() -> None:
    body = _sse_bytes(
        [
            (
                "partial",
                {
                    "text": "Remind",
                    "provider": "parakeet-eou-120m",
                    "sequence": 1,
                    "stable": False,
                },
            ),
            (
                "partial",
                {
                    "text": "Remind me",
                    "provider": "parakeet-eou-120m",
                    "sequence": 2,
                    "stable": True,
                },
            ),
            (
                "final_transcript",
                {
                    "text": "Remind me to call mom.",
                    "provider": "parakeet-eou-120m",
                    "confidence": 0.94,
                },
            ),
            (
                "reply",
                {
                    "session_id": "s1",
                    "state": "follow_up",
                    "transcript": "Remind me to call mom.",
                    "reply": "Done — added to your memory.",
                    "conversation_id": "c1",
                },
            ),
            ("done", {}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voice/utterance/stream"
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )

    partials: list[dict] = []
    finals: list[dict] = []
    async with AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mock",
    ) as client:
        result = await voice_listen(
            "s1",
            text="Remind me to call mom.",
            client=client,
            on_partial=partials.append,
            on_final=finals.append,
        )
    assert [item["text"] for item in partials] == ["Remind", "Remind me"]
    assert partials[1]["stable"] is True
    assert finals[0]["text"] == "Remind me to call mom."
    assert result["reply"] == "Done — added to your memory."


async def test_cli_voice_session_streaming_roundtrip(
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    await consent_grant("voice_enrollment", client=client)
    samples: list[str | Path] = []
    sample_bytes = b"owner-voice-session-" + b"y" * 256
    for index in range(5):
        path = tmp_path / f"session-{index}.wav"
        path.write_bytes(sample_bytes)
        samples.append(path)
    await voice_enroll(samples, liveness="live", client=client)

    wake = await voice_wake(text_hint="evie", device_id="cli-session-test", client=client)
    assert wake["session_id"]
    session_id = str(wake["session_id"])

    verified = await voice_session_verify(
        session_id,
        samples[:3],
        nonce=wake["challenge_nonce"] or "cli-verify",
        liveness="live",
        client=client,
    )
    assert verified["verified"] is True

    partials: list[dict] = []
    finals: list[dict] = []
    result = await voice_listen(
        session_id,
        text="Remind me to call mom.",
        client=client,
        on_partial=partials.append,
        on_final=finals.append,
    )
    assert result["reply"]
    assert result["transcript"] == "Remind me to call mom."
    assert finals


async def test_cli_notify_test_history_status(client: AsyncClient) -> None:
    row = await notify_send(
        "CLI test notification",
        "Delivery receipt probe from the workbench.",
        tier="useful",
        source="cli-test",
        client=client,
    )
    assert row["id"]
    assert row["status"] in {"attempted", "delivered", "failed", "suppressed"}

    rows = await notify_history(client=client, limit=10)
    assert any(item["id"] == row["id"] for item in rows)


async def test_cli_model_list_reads_gateway(client: AsyncClient) -> None:
    result = await model_list(client=client)
    assert result["provider"]
    assert isinstance(result["models"], list)


async def test_cli_people_enroll_list_forget(client: AsyncClient, tmp_path: Path) -> None:
    await consent_grant("face_enrollment", client=client)
    photos: list[str | Path] = []
    photo_bytes = b"face-photo-" + b"z" * 512
    for index in range(5):
        path = tmp_path / f"photo-{index}.jpg"
        path.write_bytes(photo_bytes)
        photos.append(path)

    enrolled = await people_enroll(
        "Sam",
        photos,
        quality=0.99,
        confidence=0.99,
        client=client,
    )
    entity_id = enrolled["enrollment"]["entity_id"]
    assert enrolled["sample_count"] == 5

    rows = await people_list(client=client)
    assert any(row["entity_id"] == entity_id for row in rows)

    manifest = await people_forget(entity_id, reason="cli test", client=client)
    assert manifest["face_enrollments_processed"] >= 1
    assert manifest["face_samples_deleted"] >= 1


async def test_cli_train_dry_run_and_status(client: AsyncClient) -> None:
    await consent_grant("adapter_fine_tuning", client=client)
    await consent_grant("training_corpus", client=client)
    build = await client.post("/v1/training/corpus/build")
    assert build.status_code == 201, build.text
    version = build.json()["snapshot"]["version"]

    dry = await train_dry_run(version, client=client)
    assert dry["mode"] == "dry_run"
    assert "passed" in dry

    rows = await train_status(client=client)
    assert isinstance(rows, list)


async def test_cli_eval_asr_dev_double_is_honest() -> None:
    report = await eval_asr()
    assert report["provider"] == "echo"
    assert report["transcript"] == "EVIE evaluation phrase: remember local AI tools."
    assert report["confidence"] == 0.0
    assert report["degraded"] is False
    assert "dev double" in report["note"]


def test_cli_word_error_rate() -> None:
    assert word_error_rate("a b c", "a b c") == 0.0
    assert abs(word_error_rate("a b c", "a c") - 1 / 3) < 1e-4
    assert word_error_rate("a b c", "") == 1.0
    assert word_error_rate("", "") == 0.0


async def test_cli_eval_asr_reports_wer_when_expected() -> None:
    report = await eval_asr(expected="EVIE evaluation phrase: remember local AI tools.")
    assert report["exact_match"] is True
    assert report["wer"] == 0.0


def test_cli_model_stats_parser() -> None:
    args = build_parser().parse_args(["model", "stats"])
    assert args.model_command == "stats"


def test_cli_workbench_info_loopback(monkeypatch) -> None:
    monkeypatch.setenv("EV_API_URL", "http://127.0.0.1:8000")
    info = workbench_info()
    assert info["url"] == "http://127.0.0.1:8000/app"
    assert info["loopback_auto_connect"] is True


def test_cli_workbench_info_remote(monkeypatch) -> None:
    monkeypatch.setenv("EV_API_URL", "http://tailscale.example:8000")
    info = workbench_info()
    assert info["url"] == "http://tailscale.example:8000/app"
    assert info["loopback_auto_connect"] is False


def test_cli_workbench_parser() -> None:
    args = build_parser().parse_args(["workbench"])
    assert args.command == "workbench"


async def test_cli_day1_script_ten_actions_or_fewer(
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    """Automate docs/CLIENTS.md §4.1.3: master key -> capture -> ask -> audit."""
    owner = await identity_owner_create("Sahaj", client=client)
    assert len(owner["recovery_codes"]) == 8

    for track in ("voice_enrollment", "training_corpus", "life_data_personalization"):
        granted = await consent_grant(track, client=client)
        assert granted["track"] == track

    samples: list[str | Path] = []
    sample_bytes = b"day1-owner-voice-" + b"w" * 256
    for index in range(5):
        path = tmp_path / f"day1-{index}.wav"
        path.write_bytes(sample_bytes)
        samples.append(path)
    enrolled = await voice_enroll(samples, liveness="live", client=client)
    assert enrolled["sample_count"] == 5

    captured = await capture(
        "I prefer fixed-term contracts for client work.",
        client=client,
    )
    assert captured["event"]["id"]

    answered = await ask_stream(
        "What do I prefer for client work?",
        client=client,
    )
    assert answered.get("conversation_id")

    found = await memories(client=client, q="fixed-term contracts", limit=10)
    assert found["total"] >= 1
    memory = found["memories"][0]
    trail = await audit(memory["id"], client=client)
    assert trail["memory"]["id"] == memory["id"]
    assert trail["source_events"]
