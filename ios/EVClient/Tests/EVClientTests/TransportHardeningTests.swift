import Foundation
import Testing

@testable import EVClient

/// Transport-hardening locks: retry policy, stream event tolerance, and the
/// session split (RPC fails fast for the offline queue; the live socket waits
/// out a Tailnet roam).
@Suite("Transport hardening")
struct TransportHardeningTests {
    @Test("retry plan truth table")
    func retryPlanTruthTable() {
        #expect(EvieRetryPlan.shouldRetry(statusCode: nil) == true)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 408) == true)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 429) == true)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 500) == true)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 503) == true)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 200) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 201) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 400) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 401) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 403) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 404) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 409) == false)
        #expect(EvieRetryPlan.shouldRetry(statusCode: 422) == false)
    }

    @Test("retry backoff math")
    func retryBackoffMath() {
        let plan = EvieRetryPlan(maxAttempts: 3, baseDelaySeconds: 1, maxDelaySeconds: 300)
        #expect(plan.canRetry == true)
        #expect(plan.nextDelaySeconds() == 1)
        let advanced = plan.advanced().advanced()
        #expect(advanced.nextDelaySeconds() == 4)
        let exhausted = advanced.advanced()
        #expect(exhausted.canRetry == false)
    }

    @Test("malformed chat event yields error and stream continues")
    func flushChatToleratesBadJSON() async throws {
        var yielded: [ChatStreamEvent] = []
        let stream = AsyncThrowingStream<ChatStreamEvent, Error> { continuation in
            EVAPIClient.flushChat(name: "delta", data: "{not json", continuation: continuation)
            EVAPIClient.flushChat(name: "status", data: #"{"stage":"thinking"}"#, continuation: continuation)
            continuation.finish()
        }
        for try await event in stream {
            yielded.append(event)
        }
        #expect(yielded.count == 2)
        guard yielded.count == 2 else { return }
        if case .error(let message) = yielded[0] {
            #expect(message.contains("Bad chat event 'delta'"))
        } else {
            Issue.record("first event should be .error, got \(yielded[0])")
        }
        #expect(yielded[1] == .status("thinking"))
    }

    @Test("chat surfaces become a HUD card only when the turn opened one")
    func flushChatKeepsAnOpenCardAndDropsAClosedPlan() async throws {
        var yielded: [ChatStreamEvent] = []
        let stream = AsyncThrowingStream<ChatStreamEvent, Error> { continuation in
            EVAPIClient.flushChat(
                name: "surfaces",
                data: #"{"schema_version":"ev.hud.lookout.v1","open":true,"windows":[]}"#,
                continuation: continuation
            )
            EVAPIClient.flushChat(
                name: "surfaces",
                data: #"{"schema_version":"ev.hud.card.v1","open":false,"title":"Hidden","body":"no","priority":0.2}"#,
                continuation: continuation
            )
            EVAPIClient.flushChat(
                name: "surfaces",
                data: #"{"schema_version":"ev.hud.card.v1","open":true,"title":"Protocols","body":"Ready","priority":0.4}"#,
                continuation: continuation
            )
            continuation.finish()
        }
        for try await event in stream {
            yielded.append(event)
        }
        #expect(yielded.count == 1)
        guard case .surface(let card) = yielded.first else {
            Issue.record("expected one open card, got \(yielded)")
            return
        }
        #expect(card.title == "Protocols")
        #expect(card.body == "Ready")
    }

    @Test("stream error wrapper normalizes terminal failures")
    func wrapStreamErrorTable() {
        let cancelled = CancellationError()
        #expect(EVAPIClient.wrapStreamError(cancelled, stream: "chat") is CancellationError)
        let urlCancelled = URLError(.cancelled)
        #expect(EVAPIClient.wrapStreamError(urlCancelled, stream: "chat") is CancellationError)
        let typed = EVAPIError.httpStatus(500, "boom")
        let passthrough = EVAPIClient.wrapStreamError(typed, stream: "chat")
        guard case EVAPIError.httpStatus(500, let body) = passthrough, body == "boom" else {
            Issue.record("typed errors must pass through untouched")
            return
        }
        let dropped = URLError(.networkConnectionLost)
        let wrapped = EVAPIClient.wrapStreamError(dropped, stream: "voice") as? EVAPIError
        guard case .transport(let message) = wrapped else {
            Issue.record("expected .transport, got \(String(describing: wrapped))")
            return
        }
        #expect(message.contains("voice stream interrupted"))
    }

    @Test("session connectivity split")
    func sessionConnectivitySplit() {
        // RPC fails fast so offline captures queue instead of stalling;
        // the live socket waits out a roam.
        #expect(EVAPIClient.voiceSession.configuration.waitsForConnectivity == false)
        #expect(EVAPIClient.liveSocketSession.configuration.waitsForConnectivity == true)
    }
}
