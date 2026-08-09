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
