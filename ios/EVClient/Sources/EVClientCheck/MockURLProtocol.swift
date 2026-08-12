import Foundation

final class MockURLProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

func mockSession() -> URLSession {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [MockURLProtocol.self]
    return URLSession(configuration: configuration)
}

func httpResponse(_ status: Int, contentLength: Int? = nil) -> HTTPURLResponse {
    var headers: [String: String] = [:]
    if let contentLength {
        headers["Content-Length"] = String(contentLength)
    }
    return HTTPURLResponse(
        url: URL(string: "https://ev.test/v1/events")!,
        statusCode: status,
        httpVersion: nil,
        headerFields: headers
    )!
}

func captureBody(eventID: String) -> String {
    """
    {
      "event": {
        "id": "\(eventID)",
        "occurred_at": "2026-08-09T12:00:00Z",
        "ingested_at": "2026-08-09T12:00:00Z",
        "source": "ios",
        "event_type": "note",
        "content": {"text": "hello"},
        "metadata": {},
        "device_id": null,
        "conversation_id": null,
        "privacy_level": "normal",
        "sha256": "abc",
        "tombstoned_at": null,
        "tombstone_reason": null
      },
      "memory_delta": []
    }
    """
}

extension URLRequest {
    func bodyData() -> Data {
        if let body = httpBody {
            return body
        }
        guard let stream = httpBodyStream else {
            return Data()
        }
        stream.open()
        defer { stream.close() }
        var buffer = [UInt8](repeating: 0, count: 4096)
        var data = Data()
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 {
                break
            }
            data.append(buffer, count: count)
        }
        return data
    }
}

func attachmentResponseJSON() -> String {
    """
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
}

func heartbeatBody(deviceID: String) -> String {
    """
    {
      "id": "hb-1",
      "device_id": "\(deviceID)",
      "reported_at": "2026-08-09T12:00:00Z",
      "status": "ok",
      "listener_state": "listening",
      "battery_percent": 71.0,
      "latency_ms": 14,
      "details": {}
    }
    """
}

func wakeBody(deviceID: String, sessionID: String) -> String {
    """
    {
      "winner": {
        "device_id": "\(deviceID)",
        "name": "iPhone",
        "score": 0.91,
        "selected": true,
        "reason": "winner"
      },
      "candidates": [
        {
          "device_id": "\(deviceID)",
          "name": "iPhone",
          "score": 0.91,
          "selected": true,
          "reason": "winner"
        }
      ],
      "state": "verifying",
      "session_id": "\(sessionID)",
      "blocked": false,
      "block_reason": null
    }
    """
}

func syncBody() -> String {
    """
    {
      "schema_version": "ev.runtime.sync.v1",
      "generated_at": "2026-08-09T12:00:00Z",
      "runtime": {
        "state": "verifying",
        "session_id": "sess-1",
        "session_state": "verifying",
        "device_id": "dev-ios",
        "quiet_hours_active": false,
        "attention": {"delivered_today": 1, "budget": 5, "remaining": 4},
        "dead_letters": {"new": 0, "retrying": 0, "discarded": 0, "resolved": 0},
        "actions_pending": 0
      },
      "devices": [
        {
          "device_id": "dev-ios",
          "name": "iPhone",
          "presence": "online",
          "listener_state": "listening",
          "battery_percent": 71.0,
          "last_seen_at": "2026-08-09T12:00:00Z",
          "last_heartbeat_at": "2026-08-09T12:00:00Z"
        }
      ],
      "events": [
        {
          "id": "evt-r1",
          "occurred_at": "2026-08-09T12:00:00Z",
          "kind": "wake",
          "device_id": "dev-ios",
          "session_id": "sess-1",
          "action_id": null
        }
      ],
      "latency": {
        "session_id": "sess-1",
        "wake_to_awake_ms": null,
        "wake_to_processing_ms": null,
        "wake_to_responding_ms": null,
        "wake_to_follow_up_ms": null
      },
      "digest": {
        "digest_id": "digest-1",
        "delivered": 3,
        "source": "runtime_daemon",
        "generated_at": "2026-08-09T07:00:00Z"
      }
    }
    """
}

func liveEventsBody(eventID: String = "live-1", channelID: String = "ch-1") -> String {
    """
    [
      {
        "id": "\(eventID)",
        "channel_id": "\(channelID)",
        "occurred_at": "2026-08-09T12:00:00Z",
        "ingested_at": "2026-08-09T12:00:00Z",
        "event_type": "focus_change",
        "payload": {
          "app": "Xcode",
          "document": "retrieval.py — EV",
          "duration_seconds": 42
        },
        "device_id": "dev-ios",
        "collector": "app-activity",
        "privacy_level": "sensitive",
        "sha256": "abc123",
        "consumed": false
      }
    ]
    """
}
