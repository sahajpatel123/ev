import Foundation
import Testing

@testable import EVClient

/// EvieEventSync: snapshot persistence, delta apply, multi-page drain,
/// reset re-bootstrap, apply-failure cursor hold, and v2 cursor validation.
@Suite("Event sync")
struct EvieEventSyncTests {
    private final class StubURLProtocol: URLProtocol, @unchecked Sendable {
        nonisolated(unsafe) static var handler: (@Sendable (URLRequest) throws -> (Int, Data))?
        nonisolated(unsafe) static var requests: [URLRequest] = []

        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            Self.requests.append(request)
            do {
                let (status, data) = try Self.handler?(request) ?? (500, Data())
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: status,
                    httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
        override func stopLoading() {}
    }

    private func makeSync(
        handler: @escaping @Sendable (URLRequest) throws -> (Int, Data)
    ) -> EvieEventSync {
        StubURLProtocol.handler = handler
        StubURLProtocol.requests = []
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        let session = URLSession(configuration: config)
        let client = EVAPIClient(
            baseURL: URL(string: "https://ev.local")!, token: "t", session: session)
        let suite = "evie-sync-test-\(UUID().uuidString)"
        let store = EvieSyncCursorStore(
            keychain: KeychainTokenStore(service: suite),
            defaults: UserDefaults(suiteName: suite)!)
        return EvieEventSync(client: client, cursors: store)
    }

    private func json(_ object: [String: Any]) -> Data {
        try! JSONSerialization.data(withJSONObject: object)
    }

    private func event(id: String, seq: Int) -> [String: Any] {
        ["id": id, "event_type": "memory.upsert", "stream_seq": seq]
    }

    @Test("bootstrap persists cursor")
    func bootstrapPersistsCursor() async throws {
        let sync = makeSync { _ in
            (200, self.json(["sync_cursor_str": "v2|7|10", "state": [:]]))
        }
        let snapshot = try await sync.bootstrap()
        #expect(snapshot.cursor == "v2|7|10")
        #expect(sync.cursors.load() == "v2|7|10")
    }

    @Test("changes advances cursor and apply is called")
    func changesAdvancesAndApplies() async throws {
        let sync = makeSync { _ in
            (200, self.json([
                "ok": true,
                "events": [self.event(id: "e1", seq: 11)],
                "next_cursor": "v2|7|11",
                "has_more": false,
            ]))
        }
        sync.cursors.save("v2|7|10")
        let applied = LockedArray<EvieSyncEvent>()
        let total = try await sync.pullToQuiescence { events in
            applied.append(contentsOf: events)
        }
        #expect(total == 1)
        #expect(applied.values.map(\.id) == ["e1"])
        #expect(sync.cursors.load() == "v2|7|11")
    }

    @Test("pullToQuiescence drains multiple pages")
    func drainsMultiPage() async throws {
        var calls = 0
        let sync = makeSync { request in
            calls += 1
            if calls == 1 {
                return (200, self.json([
                    "ok": true,
                    "events": [self.event(id: "e1", seq: 11)],
                    "next_cursor": "v2|7|11",
                    "has_more": true,
                ]))
            }
            _ = request
            return (200, self.json([
                "ok": true,
                "events": [self.event(id: "e2", seq: 12)],
                "next_cursor": "v2|7|12",
                "has_more": false,
            ]))
        }
        sync.cursors.save("v2|7|10")
        let applied = LockedArray<String>()
        let total = try await sync.pullToQuiescence { events in
            applied.append(contentsOf: events.map(\.id))
        }
        #expect(total == 2)
        #expect(applied.values == ["e1", "e2"])
        #expect(sync.cursors.load() == "v2|7|12")
        #expect(calls == 2)
    }

    @Test("reset signal re-bootstraps and continues")
    func resetReBootstraps() async throws {
        var bootstraps = 0
        let sync = makeSync { request in
            let path = request.url?.path ?? ""
            if path.hasSuffix("/sync/bootstrap") {
                bootstraps += 1
                return (200, self.json(["sync_cursor_str": "v2|8|1"]))
            }
            if bootstraps > 0 {
                return (200, self.json([
                    "ok": true,
                    "events": [self.event(id: "fresh", seq: 1)],
                    "next_cursor": "v2|8|1",
                    "has_more": false,
                ]))
            }
            return (200, self.json([
                "ok": false, "error": "CURSOR_INVALID", "reset_required": true,
            ]))
        }
        sync.cursors.save("stale-cursor")
        let total = try await sync.pullToQuiescence { _ in }
        #expect(bootstraps == 1)
        #expect(total == 1)
        #expect(sync.cursors.load() == "v2|8|1")
    }

    @Test("cursor is not advanced when apply throws")
    func cursorHeldOnApplyFailure() async throws {
        let sync = makeSync { _ in
            (200, self.json([
                "ok": true,
                "events": [self.event(id: "e1", seq: 11)],
                "next_cursor": "v2|7|11",
                "has_more": false,
            ]))
        }
        sync.cursors.save("v2|7|10")
        struct ApplyFailed: Error {}
        do {
            _ = try await sync.pullToQuiescence { _ in throw ApplyFailed() }
            Issue.record("expected apply to throw")
        } catch is ApplyFailed {
        } catch {
            Issue.record("unexpected error: \(error)")
        }
        #expect(sync.cursors.load() == "v2|7|10")
    }

    @Test("isV2Cursor validation")
    func v2CursorValidation() {
        #expect(EvieSyncCursorStore.isV2Cursor("v2|7|10"))
        #expect(!EvieSyncCursorStore.isV2Cursor(nil))
        #expect(!EvieSyncCursorStore.isV2Cursor(""))
        #expect(!EvieSyncCursorStore.isV2Cursor("v1|7|10"))
        #expect(!EvieSyncCursorStore.isV2Cursor("v2|7"))
        #expect(!EvieSyncCursorStore.isV2Cursor("v2|7|abc"))
        #expect(!EvieSyncCursorStore.isV2Cursor("v2||10"))
    }
}

private final class LockedArray<Element>: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [Element] = []
    var values: [Element] { lock.withLock { storage } }
    func append(contentsOf elements: [Element]) {
        lock.withLock { storage.append(contentsOf: elements) }
    }
}
