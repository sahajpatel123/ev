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

if failures.isEmpty {
    print("EVClientCheck: all checks passed")
    exit(0)
} else {
    print("EVClientCheck: \(failures.count) failure(s)")
    exit(1)
}
