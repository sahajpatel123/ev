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

func httpResponse(_ status: Int) -> HTTPURLResponse {
    HTTPURLResponse(
        url: URL(string: "https://ev.test/v1/events")!,
        statusCode: status,
        httpVersion: nil,
        headerFields: nil
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
      }
    }
    """
}
