import Foundation
import EVClient

var failures: [String] = []

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        failures.append(message)
        print("FAIL: \(message)")
    }
}

let client = EVAPIClient(
    baseURL: URL(string: "https://ev.test")!,
    token: "test-key",
    session: mockSession()
)

// 1. HUD card: decode, validate, render consistently.
do {
    let json = """
    {
      "schema_version": "ev.hud.card.v1",
      "generated_at": "2026-08-09T12:00:00Z",
      "title": "EV status",
      "body": "Goal: ship EV | Readiness 68",
      "priority": 0.4,
      "meta": {"readiness": 68, "band": "good", "pending_alerts": 1}
    }
    """
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let card = try decoder.decode(HUDCard.self, from: Data(json.utf8))
    try card.validate()
    expect(card.schemaVersion == "ev.hud.card.v1", "HUD schema version decoded")
    expect(
        card.renderText().contains("[ev.hud.card.v1] EV status"),
        "HUD render includes schema + title"
    )
    expect(card.renderText().contains("Readiness 68"), "HUD render includes body")
    print("ok: HUD card decode/validate/render")
} catch {
    failures.append("HUD card: \(error)")
    print("FAIL: HUD card: \(error)")
}

let unsupported = HUDCard(
    schemaVersion: "ev.hud.card.v9",
    generatedAt: "2026-08-09T12:00:00Z",
    title: "x",
    body: "y"
)
do {
    try unsupported.validate()
    failures.append("unsupported HUD schema accepted")
    print("FAIL: unsupported HUD schema accepted")
} catch HUDCardError.unsupportedSchema(let version) {
    expect(version == "ev.hud.card.v9", "unsupported schema version reported")
    print("ok: unsupported HUD schema rejected")
} catch {
    failures.append("unsupported HUD: \(error)")
    print("FAIL: unsupported HUD: \(error)")
}

// 7. Tactical quick card: decode, validate, render consistently.
do {
    let json = """
    {
      "schema_version": "ev.hud.quickcard.v1",
      "generated_at": "2026-08-09T12:00:00Z",
      "objective": "Renegotiation with X",
      "summary": "2 prior fixed-term wins; cap scope in writing.",
      "next_action": "Send the draft cap",
      "top_risk": "Scope creep",
      "people_count": 3,
      "options_count": 2,
      "decision_history_count": 4,
      "meta": {}
    }
    """
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let card = try decoder.decode(HUDQuickCard.self, from: Data(json.utf8))
    try card.validate()
    expect(
        card.renderText().hasPrefix("[ev.hud.quickcard.v1] Renegotiation with X"),
        "quick card render includes schema + objective"
    )
    expect(card.renderText().contains("next: Send the draft cap"), "quick card render includes next action")
    print("ok: quick card decode/validate/render")
} catch {
    failures.append("quick card: \(error)")
    print("FAIL: quick card: \(error)")
}

// 8. Attachment upload: multipart body carries the file; response decodes.
do {
    var receivedBody = ""
    var receivedContentType = ""
    var receivedStatus = 0
    MockURLProtocol.handler = { request in
        var bodyData = request.httpBody ?? Data()
        if bodyData.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                bodyData.append(buffer, count: count)
            }
        }
        receivedBody = String(data: bodyData, encoding: .utf8) ?? ""
        receivedContentType = request.value(forHTTPHeaderField: "Content-Type") ?? ""
        receivedStatus = 201
        let response = """
        {
          "attachment": {
            "id": "att-1",
            "event_id": "evt-att",
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "size_bytes": 11,
            "storage_key": "attachments/x.bin",
            "sha256": "abc123",
            "created_at": "2026-08-09T12:00:00Z"
          },
          "event": {
            "id": "evt-att",
            "occurred_at": "2026-08-09T12:00:00Z",
            "ingested_at": "2026-08-09T12:00:00Z",
            "source": "ios",
            "event_type": "file",
            "content": {"filename": "photo.jpg", "content_type": "image/jpeg"},
            "metadata": {},
            "device_id": null,
            "conversation_id": null,
            "privacy_level": "normal",
            "sha256": "abc",
            "tombstoned_at": null,
            "tombstone_reason": null
          }
        }
        """
        let responseData = Data(response.utf8)
        print("mock: handler invoked, request=\(request.url?.path ?? "?") body=\(receivedBody.count) bytes response=\(responseData.count) bytes")
        return (httpResponse(201, contentLength: responseData.count), responseData)
    }
    do {
        let result = try await client.attach(
            filename: "photo.jpg",
            contentType: "image/jpeg",
            data: Data("photo-bytes".utf8)
        )
        expect(receivedStatus == 201, "attachment request reached mock")
        expect(receivedContentType.contains("multipart/form-data"), "attachment uses multipart content type")
        expect(receivedBody.contains("name=\"file\""), "attachment body has file part")
        expect(receivedBody.contains("filename=\"photo.jpg\""), "attachment body has filename")
        expect(receivedBody.contains("photo-bytes"), "attachment body contains file data")
        expect(result.attachment.filename == "photo.jpg", "attachment filename decoded")
        expect(result.attachment.sizeBytes == 11, "attachment size decoded")
        expect(result.event.source == "ios", "attachment event source decoded")
        print("ok: attachment upload")
    } catch {
        failures.append("attachment: \(error)")
        print("FAIL: attachment: \(error) (status=\(receivedStatus), body=\(receivedBody.prefix(120)))")
    }
} catch {
    failures.append("attachment: \(error)")
    print("FAIL: attachment: \(error)")
}

// 2. Capture sends Idempotency-Key and returns the event.
do {
    var capturedKey: String?
    MockURLProtocol.handler = { request in
        capturedKey = request.value(forHTTPHeaderField: "Idempotency-Key")
        return (httpResponse(201), Data(captureBody(eventID: "evt-1").utf8))
    }
    let result = try await client.capture(
        payload: CapturePayload(text: "hello"),
        idempotencyKey: "ios-key-1"
    )
    expect(capturedKey == "ios-key-1", "capture sent Idempotency-Key")
    expect(!result.duplicate, "capture 201 is not a duplicate")
    expect(result.event?.id == "evt-1", "capture returned event id")
    expect(result.event?.source == "ios", "capture returned source")
    print("ok: capture with idempotency key")
} catch {
    failures.append("capture: \(error)")
    print("FAIL: capture: \(error)")
}

// 3. 409 duplicate is surfaced, not thrown.
do {
    MockURLProtocol.handler = { _ in
        (httpResponse(409), Data(captureBody(eventID: "evt-dup").utf8))
    }
    let result = try await client.capture(
        payload: CapturePayload(text: "hello"),
        idempotencyKey: "ios-key-dup"
    )
    expect(result.duplicate, "409 capture flagged duplicate")
    expect(result.event?.id == "evt-dup", "duplicate returns existing event")
    print("ok: 409 duplicate handling")
} catch {
    failures.append("duplicate: \(error)")
    print("FAIL: duplicate: \(error)")
}

// 4. Offline queue: 201 syncs, 409 drops, network failure preserves.
do {
    let store = MemoryCaptureQueueStore()
    let queue = OfflineCaptureQueue(store: store)
    _ = try queue.enqueue(CapturePayload(text: "one"), idempotencyKey: "k1")
    _ = try queue.enqueue(CapturePayload(text: "two"), idempotencyKey: "k2")
    MockURLProtocol.handler = { request in
        let key = request.value(forHTTPHeaderField: "Idempotency-Key")
        let status = key == "k1" ? 201 : 409
        return (httpResponse(status), Data(captureBody(eventID: "evt").utf8))
    }
    let summary = await queue.sync(using: client)
    expect(summary.synced == 1, "queue synced one capture")
    expect(summary.dropped == 1, "queue dropped one duplicate")
    expect(summary.remaining == 0, "queue drained")
    let pendingAfterSync = try queue.pending()
    expect(pendingAfterSync.isEmpty, "queue empty after sync")

    _ = try queue.enqueue(CapturePayload(text: "three"), idempotencyKey: "k3")
    MockURLProtocol.handler = { _ in
        throw URLError(.notConnectedToInternet)
    }
    let failed = await queue.sync(using: client)
    expect(failed.remaining == 1, "network failure preserved queue")
    let pendingAfterFailure = try queue.pending()
    expect(pendingAfterFailure.count == 1, "pending count after failure")
    print("ok: offline queue sync/preserve")
} catch {
    failures.append("offline queue: \(error)")
    print("FAIL: offline queue: \(error)")
}

// 5. Offline queue: 422 quarantines.
do {
    let store = MemoryCaptureQueueStore()
    let queue = OfflineCaptureQueue(store: store)
    _ = try queue.enqueue(CapturePayload(text: "bad"), idempotencyKey: "k-bad")
    MockURLProtocol.handler = { _ in
        (httpResponse(422), Data("{\"detail\":\"invalid\"}".utf8))
    }
    let summary = await queue.sync(using: client)
    expect(summary.quarantined == 1, "queue quarantined invalid capture")
    expect(summary.remaining == 0, "quarantined capture removed from queue")
    expect(store.quarantinedCount() == 1, "quarantine store has record")
    print("ok: offline queue quarantine")
} catch {
    failures.append("quarantine: \(error)")
    print("FAIL: quarantine: \(error)")
}

// 6. HUD endpoint renders a validated card.
do {
    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/hud/card", "HUD endpoint path")
        let body = """
        {
          "schema_version": "ev.hud.card.v1",
          "generated_at": "2026-08-09T12:00:00Z",
          "title": "EV status",
          "body": "No active signals. EV is watching.",
          "priority": 0.0,
          "meta": {}
        }
        """
        return (httpResponse(200), Data(body.utf8))
    }
    let card = try await client.hudCard()
    expect(
        card.renderText().hasPrefix("[ev.hud.card.v1] EV status"),
        "HUD endpoint render"
    )
    print("ok: HUD endpoint")
} catch {
    failures.append("HUD endpoint: \(error)")
    print("FAIL: HUD endpoint: \(error)")
}

// 8. Runtime listener: heartbeat, wake arbitration, sync snapshot.
do {
    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/runtime/heartbeat", "heartbeat path")
        return (httpResponse(201), Data(heartbeatBody(deviceID: "dev-ios").utf8))
    }
    let listener = RuntimeListener(client: client)
    let heartbeat = try await listener.heartbeat(
        deviceID: "dev-ios",
        batteryPercent: 71.0,
        latencyMs: 14
    )
    expect(heartbeat.deviceId == "dev-ios", "heartbeat device id")
    expect(heartbeat.status == "ok", "heartbeat status")
    expect(heartbeat.listenerState == "listening", "heartbeat listener state")
    expect(heartbeat.batteryPercent == 71.0, "heartbeat battery")
    print("ok: runtime heartbeat")

    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/runtime/wake", "wake path")
        return (httpResponse(200), Data(wakeBody(deviceID: "dev-ios", sessionID: "sess-1").utf8))
    }
    let wake = try await listener.wake(deviceID: "dev-ios", signalScore: 0.9, priority: 0.8)
    expect(wake.state == "verifying", "wake state")
    expect(wake.winner?.deviceId == "dev-ios", "wake winner")
    expect(wake.sessionId == "sess-1", "wake session id")
    expect(wake.blocked == false, "wake not blocked")
    print("ok: wake arbitration")

    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/runtime/sync", "sync path")
        return (httpResponse(200), Data(syncBody().utf8))
    }
    let sync = try await listener.syncState()
    expect(sync.schemaVersion == "ev.runtime.sync.v1", "sync schema version")
    expect(sync.runtime.state == "verifying", "sync runtime state")
    expect(sync.runtime.sessionId == "sess-1", "sync session id")
    expect(sync.devices.first?.deviceId == "dev-ios", "sync device")
    expect(sync.events.first?.kind == "wake", "sync event feed")
    expect(sync.latency.wakeToAwakeMs == nil, "sync latency while verifying")
    print("ok: runtime sync snapshot")
} catch {
    failures.append("runtime listener: \(error)")
    print("FAIL: runtime listener: \(error)")
}

if failures.isEmpty {
    print("EVClientCheck: all checks passed")
    exit(0)
} else {
    print("EVClientCheck: \(failures.count) failure(s)")
    exit(1)
}
