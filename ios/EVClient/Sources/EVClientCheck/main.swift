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
        let responseData = Data(attachmentResponseJSON().utf8)
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

// 8b. HUD briefing / focus / route: decode, validate, render.
do {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase

    let briefingJSON = """
    {
      "schema_version": "ev.hud.briefing.v1",
      "generated_at": "2026-08-10T12:00:00Z",
      "objective": "Renegotiation with X",
      "context": "2 prior fixed-term wins",
      "recommendation": "fixed + milestones",
      "talking_points": ["cap scope", "offer terms"]
    }
    """
    let briefing = try decoder.decode(HUDBriefing.self, from: Data(briefingJSON.utf8))
    try briefing.validate()
    expect(
        briefing.renderText().hasPrefix("[ev.hud.briefing.v1] Renegotiation with X"),
        "briefing render includes schema + objective"
    )

    let focusJSON = """
    {
      "schema_version": "ev.hud.focus.v1",
      "generated_at": "2026-08-10T12:00:00Z",
      "focus": null,
      "locked": false,
      "context": "Ship EV",
      "next_action": "Pick the first milestone",
      "meta": {}
    }
    """
    let focus = try decoder.decode(HUDFocus.self, from: Data(focusJSON.utf8))
    try focus.validate()
    expect(
        focus.renderText().contains("[ev.hud.focus.v1] focus open"),
        "focus render includes schema"
    )

    let routeJSON = """
    {
      "schema_version": "ev.hud.route.v1",
      "generated_at": "2026-08-10T12:00:00Z",
      "destination": "Studio",
      "leave_by": "10:45",
      "travel_time_minutes": 25,
      "prep_checklist": ["laptop", "brief"]
    }
    """
    let route = try decoder.decode(HUDRoute.self, from: Data(routeJSON.utf8))
    try route.validate()
    expect(
        route.renderText().hasPrefix("[ev.hud.route.v1] Studio"),
        "route render includes schema + destination"
    )
    print("ok: HUD briefing/focus/route decode/validate/render")
} catch {
    failures.append("HUD briefing/focus/route: \(error)")
    print("FAIL: HUD briefing/focus/route: \(error)")
}

// 9. Attachment offline queue: enqueue -> sync uploads multipart; missing file quarantines.
do {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let fileURL = directory.appendingPathComponent("offline-photo.jpg")
    try Data("offline-photo-bytes".utf8).write(to: fileURL)

    let store = MemoryCaptureQueueStore()
    let queue = OfflineCaptureQueue(store: store)
    _ = try queue.enqueueAttachment(filePath: fileURL.path, contentType: "image/jpeg")

    MockURLProtocol.handler = { request in
        let body = String(data: request.bodyData(), encoding: .utf8) ?? ""
        expect(body.contains("name=\"file\""), "attachment queue sends multipart")
        expect(body.contains("offline-photo-bytes"), "attachment queue sends file bytes")
        let responseData = Data(attachmentResponseJSON().utf8)
        return (httpResponse(201, contentLength: responseData.count), responseData)
    }
    let summary = await queue.sync(using: client)
    expect(summary.synced == 1, "attachment queue synced one capture")
    expect(summary.remaining == 0, "attachment queue drained")
    let pendingAfterSync = try queue.pending()
    expect(pendingAfterSync.isEmpty, "attachment queue empty after sync")

    _ = try queue.enqueueAttachment(filePath: directory.appendingPathComponent("gone.jpg").path)
    let failed = await queue.sync(using: client)
    expect(failed.quarantined == 1, "missing attachment file quarantined")
    expect(failed.remaining == 0, "missing-file record removed from queue")
    expect(store.quarantinedCount() == 1, "missing-file quarantine stored")
    let pendingAfterQuarantine = try queue.pending()
    expect(pendingAfterQuarantine.isEmpty, "missing-file queue drained")
    print("ok: attachment offline queue")
} catch {
    failures.append("attachment queue: \(error)")
    print("FAIL: attachment queue: \(error)")
}

// 10. Voice session: verify + utterance decode; utterance posts text.
do {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let verifyJSON = """
    {
      "session_id": "vs-1",
      "state": "awake",
      "verified": true,
      "confidence": 0.97,
      "reason": "voiceprint match"
    }
    """
    let verify = try decoder.decode(VoiceSessionVerifyResponse.self, from: Data(verifyJSON.utf8))
    expect(verify.verified, "voice verify decodes verified flag")
    expect(verify.sessionId == "vs-1", "voice verify decodes session id")

    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/voice/utterance", "utterance endpoint path")
        let body = String(data: request.bodyData(), encoding: .utf8) ?? ""
        expect(body.contains("\"session_id\":\"vs-1\""), "utterance body has session id")
        expect(body.contains("\"text\":\"hello EV\""), "utterance body has text")
        let response = """
        {
          "session_id": "vs-1",
          "state": "responding",
          "transcript": "hello EV",
          "transcript_confidence": 0.9,
          "reply": "Hi! What do you need?",
          "conversation_id": null,
          "tts": null,
          "style": null,
          "model": "mock",
          "context_tokens": 120,
          "memory_deltas": []
        }
        """
        let responseData = Data(response.utf8)
        return (httpResponse(200, contentLength: responseData.count), responseData)
    }
    let utterance = try await client.utterance(sessionId: "vs-1", text: "hello EV")
    expect(utterance.reply == "Hi! What do you need?", "utterance reply decoded")
    expect(utterance.state == "responding", "utterance state decoded")
    print("ok: voice session verify + utterance")
} catch {
    failures.append("voice session: \(error)")
    print("FAIL: voice session: \(error)")
}

// 11. Watch complication stub: HUD card/quickcard -> title + <=2 lines.
do {
    let card = HUDCard(
        schemaVersion: "ev.hud.card.v1",
        generatedAt: "2026-08-10T12:00:00Z",
        title: "EV status",
        body: "Goal: ship EV | Readiness 68 | 2 alerts",
        priority: 0.4
    )
    let layout = WatchComplicationStub.render(card)
    expect(layout.title == "EV status", "complication title from HUD card")
    expect(layout.lines.count <= 2, "complication keeps at most two lines")
    expect(layout.lines.first == "Goal: ship EV", "complication first line")

    let quick = HUDQuickCard(
        schemaVersion: "ev.hud.quickcard.v1",
        generatedAt: "2026-08-10T12:00:00Z",
        objective: "Renegotiation",
        summary: "2 prior fixed-term wins",
        nextAction: "Send draft cap"
    )
    let quickLayout = WatchComplicationStub.renderQuickCard(quick)
    expect(quickLayout.title == "Renegotiation", "complication title from quick card")
    expect(quickLayout.lines.count == 2, "complication quick-card lines")
    print("ok: watch complication stub")
} catch {
    failures.append("watch complication stub: \(error)")
    print("FAIL: watch complication stub: \(error)")
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
    expect(sync.digest?.digestId == "digest-1", "sync digest id")
    expect(sync.digest?.delivered == 3, "sync digest delivered count")
    print("ok: runtime sync snapshot")
} catch {
    failures.append("runtime listener: \(error)")
    print("FAIL: runtime listener: \(error)")
}

// 11. Live collector: app-activity / screen-time data model + upload path.
do {
    var receivedPath: String?
    var receivedBody = ""
    MockURLProtocol.handler = { request in
        receivedPath = request.url?.path
        receivedBody = String(data: request.bodyData(), encoding: .utf8) ?? ""
        return (httpResponse(201), Data(liveEventsBody().utf8))
    }

    let collector = LiveCollector(client: client, deviceID: "dev-ios")
    let sample = LiveActivitySample(
        appName: "Xcode",
        documentName: "retrieval.py — EV",
        category: "development",
        startedAt: "2026-08-09T12:00:00Z",
        durationSeconds: 42
    )
    let batchEvents = try await collector.upload(sample: sample)
    expect(receivedPath == "/v1/live/events", "live batch upload path")
    expect(receivedBody.contains("\"channel\":\"app-activity\""), "live batch channel")
    expect(receivedBody.contains("\"kind\":\"app\""), "live batch kind")
    expect(receivedBody.contains("\"privacy_level\":\"sensitive\""), "live batch privacy")
    expect(receivedBody.contains("\"event_type\":\"focus_change\""), "live batch event type")
    expect(receivedBody.contains("Xcode"), "live batch carries app name")
    expect(receivedBody.contains("retrieval.py"), "live batch carries document name")
    expect(receivedBody.contains("dev-ios"), "live batch carries device id")
    expect(batchEvents.first?.id == "live-1", "live batch response decodes")
    expect(batchEvents.first?.privacyLevel == "sensitive", "live batch response privacy")

    MockURLProtocol.handler = { request in
        receivedPath = request.url?.path
        receivedBody = String(data: request.bodyData(), encoding: .utf8) ?? ""
        return (httpResponse(201), Data(liveEventsBody(channelID: "ch-ios-screen").utf8))
    }
    let channelEvents = try await collector.upload(sample: sample, channelID: "ch-ios-screen")
    expect(
        receivedPath == "/v1/live/channels/ch-ios-screen/events",
        "live channel upload path"
    )
    expect(receivedBody.contains("\"event_type\":\"focus_change\""), "channel body carries events")
    expect(receivedBody.contains("dev-ios"), "channel body carries device id")
    expect(channelEvents.first?.channelId == "ch-ios-screen", "live channel response decodes")
    print("ok: live collector data model + upload path")
} catch {
    failures.append("live collector: \(error)")
    print("FAIL: live collector: \(error)")
}

// 12. Voice utterance accepts audio_b64 and decodes typed TTS/style.
do {
    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/voice/utterance", "audio utterance endpoint path")
        let body = String(data: request.bodyData(), encoding: .utf8) ?? ""
        expect(body.contains("\"audio_b64\":\"UENG\""), "utterance body has audio_b64")
        expect(body.contains("\"language\":\"en\""), "utterance body has language")
        let response = """
        {
          "session_id": "vs-2",
          "state": "responding",
          "transcript": "hello EV",
          "transcript_confidence": 0.91,
          "transcript_degraded": false,
          "transcript_provider": "mock",
          "reply": "Hi!",
          "conversation_id": null,
          "tts": {
            "provider": "piper",
            "audio_ref": "voice/abc.wav",
            "content_type": "audio/wav",
            "ssml": null,
            "duration_ms": 1400,
            "degraded": false
          },
          "style": {
            "urgency": 0.1,
            "warmth": 0.7,
            "brevity": 0.5,
            "mode": "casual",
            "length_target": "one to two sentences",
            "directness": "low to medium"
          },
          "model": "mock",
          "context_tokens": 120,
          "memory_deltas": []
        }
        """
        return (httpResponse(200), Data(response.utf8))
    }
    let result = try await client.utterance(
        sessionId: "vs-2",
        audioB64: "UENG",
        language: "en"
    )
    expect(result.tts?.provider == "piper", "typed tts provider decoded")
    expect(result.tts?.audioRef == "voice/abc.wav", "typed tts audio_ref decoded")
    expect(result.tts?.durationMs == 1400, "typed tts duration decoded")
    expect(result.style?.warmth == 0.7, "typed style decoded")
    expect(result.transcriptProvider == "mock", "transcript provider decoded")
    print("ok: voice utterance audio_b64 + typed tts/style")
} catch {
    failures.append("audio utterance: \(error)")
    print("FAIL: audio utterance: \(error)")
}

// 13. Chat SSE stream: delta, refined, done, error surfaces.
do {
    let stream = """
    event: memory-delta
    data: {"action":"created","memory_type":"observation","id":"m1","text":"streamed"}

    event: provenance
    data: {"memory_id":"m1","text":"streamed","memory_type":"observation","score":0.8}

    event: delta
    data: {"text":"Hel","final":false}

    event: delta
    data: {"text":"lo","final":true}

    event: refined
    data: {"text":"Hello","replaces":true}

    event: done
    data: {"conversation_id":"c1","context_tokens":12,"context_depth":"standard","request_id":"r1","model":"mock"}
    """
    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/chat", "chat stream path")
        expect(request.value(forHTTPHeaderField: "Accept") == "text/event-stream", "chat stream accept header")
        return (httpResponse(200), Data(stream.utf8))
    }
    var events: [ChatStreamEvent] = []
    for try await event in client.askStream("hi") {
        events.append(event)
    }
    expect(events.count == 6, "chat stream parsed 6 events")
    guard case .memoryDelta(let delta)? = events.first else {
        failures.append("chat stream first event is memory delta")
        print("FAIL: chat stream first event is memory delta")
        throw NSError(domain: "test", code: 1)
    }
    expect(delta.id == "m1", "chat stream memory delta id")
    expect(events.contains { if case .refined("Hello") = $0 { return true }; return false }, "chat stream refined event")
    guard case .done(let done)? = events.last else {
        failures.append("chat stream last event is done")
        print("FAIL: chat stream last event is done")
        throw NSError(domain: "test", code: 2)
    }
    expect(done.conversationId == "c1", "chat stream done conversation id")
    expect(done.model == "mock", "chat stream done model")
    print("ok: chat SSE stream")
} catch {
    failures.append("chat stream: \(error)")
    print("FAIL: chat stream: \(error)")
}

// 14. Voice SSE stream: partial, final transcript, reply, done.
do {
    let stream = """
    event: partial
    data: {"text":"hel","provider":"mock","sequence":1,"stable":false,"confidence":0.8,"degraded":false,"timestamp_ms":10}

    event: final_transcript
    data: {"text":"hello","confidence":0.92,"provider":"mock","degraded":false,"audio_ref":"voice/in.wav"}

    event: tts_chunk
    data: {"index":0,"text":"Hi!","audio_b64":"UENG","content_type":"audio/wav"}

    event: reply
    data: {"session_id":"vs-3","state":"responding","transcript":"hello","transcript_confidence":0.92,"reply":"Hi!","conversation_id":null,"tts":{"provider":"piper","audio_ref":"voice/out.wav","content_type":"audio/wav","ssml":null,"duration_ms":900,"degraded":false},"style":null,"model":"mock","context_tokens":10,"memory_deltas":[]}

    event: done
    data: {}
    """
    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/voice/utterance/stream", "voice stream path")
        return (httpResponse(200), Data(stream.utf8))
    }
    var events: [VoiceStreamEvent] = []
    for try await event in client.streamUtterance(sessionId: "vs-3", audioB64: "UENG") {
        events.append(event)
    }
    expect(events.count == 5, "voice stream parsed 5 events")
    guard case .partial(let partial)? = events.first else {
        failures.append("voice stream first event is partial")
        print("FAIL: voice stream first event is partial")
        throw NSError(domain: "test", code: 3)
    }
    expect(partial.text == "hel", "voice stream partial text")
    guard case .transcript(let transcript)? = events.dropFirst().first else {
        failures.append("voice stream second event is transcript")
        print("FAIL: voice stream second event is transcript")
        throw NSError(domain: "test", code: 4)
    }
    expect(transcript.text == "hello", "voice stream transcript text")
    guard case .ttsChunk(let chunk)? = events.dropFirst(2).first else {
        failures.append("voice stream third event is tts_chunk")
        print("FAIL: voice stream third event is tts_chunk")
        throw NSError(domain: "test", code: 4)
    }
    expect(chunk.audioB64 == "UENG", "voice stream tts chunk audio")
    guard case .reply(let reply)? = events.dropFirst(3).first else {
        failures.append("voice stream fourth event is reply")
        print("FAIL: voice stream fourth event is reply")
        throw NSError(domain: "test", code: 5)
    }
    expect(reply.tts?.audioRef == "voice/out.wav", "voice stream reply tts audio_ref")
    guard case .done = events.last else {
        failures.append("voice stream last event is done")
        print("FAIL: voice stream last event is done")
        throw NSError(domain: "test", code: 6)
    }
    print("ok: voice SSE stream")
} catch {
    failures.append("voice stream: \(error)")
    print("FAIL: voice stream: \(error)")
}

// 14b. Voice wake carries a local audio_ref for the wake engine.
do {
    MockURLProtocol.handler = { request in
        expect(request.url?.path == "/v1/voice/wake", "voice wake path")
        let body = String(data: request.bodyData(), encoding: .utf8) ?? ""
        expect(body.contains("audio_ref"), "voice wake audio_ref")
        expect(body.contains("ev-smoke-wake.wav"), "voice wake audio_ref value")
        expect(body.contains("\"device_id\":\"dev-audio\""), "voice wake device id")
        let response = """
        {
          "session_id": null,
          "state": "idle",
          "owner_enrolled": false,
          "challenge_nonce": null,
          "challenge_phrase": null,
          "message": "no session"
        }
        """
        return (httpResponse(201), Data(response.utf8))
    }
    let wake = try await client.wakeVoice(
        deviceId: "dev-audio",
        audioRef: "/tmp/ev-smoke-wake.wav"
    )
    expect(wake.state == "idle", "voice wake state decoded")
    expect(wake.sessionId == nil, "voice wake no session decoded")
    print("ok: voice wake audio_ref")
} catch {
    failures.append("voice wake: \(error)")
    print("FAIL: voice wake: \(error)")
}

// 15. Keychain token store (skip silently when an unsigned CLT binary is
// denied keychain access; that is an environment limitation, not a client bug).
do {
    let store = KeychainTokenStore(service: "ev.clientcheck.\(UUID().uuidString)")
    try store.save(token: "sekret-token", account: "test")
    let loaded = try store.load(account: "test")
    expect(loaded == "sekret-token", "keychain token round-trip")
    try store.delete(account: "test")
    expect((try? store.load(account: "test")) == nil, "keychain token deleted")
    print("ok: keychain token store")
} catch {
    print("skip: keychain unavailable to unsigned CLT binary (\(error))")
}

// 16. LIFE access: permission report POST + SMS URL builder.
do {
    MockURLProtocol.handler = { request in
        expect(
            request.url?.path == "/v1/devices/dev-1/permissions",
            "permission report path"
        )
        let body = String(data: request.bodyData(), encoding: .utf8) ?? ""
        expect(body.contains("\"platform\":\"macos\""), "permission report platform")
        expect(body.contains("\"permission\":\"microphone\""), "permission report entry")
        return (httpResponse(202), Data("{}".utf8))
    }
    let report = EVPermissionReport(
        platform: "macos",
        deviceId: "dev-1",
        permissions: [
            EVPermissionEntry(permission: "microphone", state: "granted"),
        ]
    )
    let posted = try await client.postPermissionReport(report, deviceID: "dev-1")
    expect(posted, "permission report accepted")

    let sms = EVMessageURLs.smsURL(recipients: ["+123456"], body: "hi EV")
    expect(sms?.scheme == "sms", "sms url scheme")
    expect(sms?.query?.contains("body=hi%20EV") == true, "sms url body")
    print("ok: LIFE access APIs")
} catch {
    failures.append("life access: \(error)")
    print("FAIL: life access: \(error)")
}

if failures.isEmpty {
    print("EVClientCheck: all checks passed")
    exit(0)
} else {
    print("EVClientCheck: \(failures.count) failure(s)")
    exit(1)
}
