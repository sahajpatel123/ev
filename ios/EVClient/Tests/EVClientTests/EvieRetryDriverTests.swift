import Foundation
import Testing

@testable import EVClient

/// EvieRetryDriver: capped exponential backoff with ±25% jitter.
@Suite("Retry driver")
struct EvieRetryDriverTests {
    @Test("backoff doubles per attempt, capped")
    func backoffCap() {
        let d = EvieRetryDriver(baseDelaySeconds: 1, maxDelaySeconds: 8, maxAttempts: 5)
        #expect(d.delay(attempt: 0, jitterAnchor: 0.5) == 1.0)
        #expect(d.delay(attempt: 1, jitterAnchor: 0.5) == 2.0)
        #expect(d.delay(attempt: 2, jitterAnchor: 0.5) == 4.0)
        #expect(d.delay(attempt: 10, jitterAnchor: 0.5) == 8.0)
    }

    @Test("jitter stays within ±25% and respects cap")
    func jitterBounds() {
        let d = EvieRetryDriver(baseDelaySeconds: 4, maxDelaySeconds: 100, maxAttempts: 5)
        for anchor in stride(from: 0.0, through: 0.99, by: 0.01) {
            let got = d.delay(attempt: 0, jitterAnchor: anchor)
            #expect(got >= 3.0 && got <= 5.0, "anchor \(anchor) -> \(got)")
        }
        // Cap binds even at max upward jitter.
        let capped = EvieRetryDriver(baseDelaySeconds: 90, maxDelaySeconds: 100, maxAttempts: 5)
        #expect(capped.delay(attempt: 3, jitterAnchor: 0.99) == 100.0)
    }

    @Test("verdict retries transport failure and 5xx, not contract codes")
    func verdictTable() {
        let d = EvieRetryDriver(baseDelaySeconds: 1, maxDelaySeconds: 60, maxAttempts: 3)
        #expect(d.verdict(statusCode: nil, attempt: 0, jitterAnchor: 0.5).shouldRetry)
        #expect(d.verdict(statusCode: 503, attempt: 0, jitterAnchor: 0.5).shouldRetry)
        #expect(d.verdict(statusCode: 429, attempt: 0, jitterAnchor: 0.5).shouldRetry)
        #expect(!d.verdict(statusCode: 409, attempt: 0, jitterAnchor: 0.5).shouldRetry)
        #expect(!d.verdict(statusCode: 422, attempt: 0, jitterAnchor: 0.5).shouldRetry)
        #expect(!d.verdict(statusCode: 401, attempt: 0, jitterAnchor: 0.5).shouldRetry)
        #expect(!d.verdict(statusCode: 503, attempt: 3, jitterAnchor: 0.5).shouldRetry)
    }
}
