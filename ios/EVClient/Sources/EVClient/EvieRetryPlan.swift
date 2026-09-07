// Cycle EAC-18 — iPhone offline retry plan.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Decides when the offline queue on both iPhones (16 Pro + SE) should retry a
/// failed capture sync over flaky phone networks. Transport failures, 408,
/// 429, and 5xx are retryable with capped exponential backoff; contract
/// outcomes (201 synced, 409 duplicate, 422 quarantined) and client errors
/// (400/401/403/404) never retry. Pure value type, Foundation only, so CLT
/// compiles. No backend change.

import Foundation

public struct EvieRetryPlan: Equatable, Sendable {
    public var attempt: Int
    public var maxAttempts: Int
    public var baseDelaySeconds: Double
    public var maxDelaySeconds: Double

    public init(
        attempt: Int = 0,
        maxAttempts: Int = 5,
        baseDelaySeconds: Double = 2,
        maxDelaySeconds: Double = 300
    ) {
        self.attempt = max(0, attempt)
        self.maxAttempts = max(1, maxAttempts)
        self.baseDelaySeconds = max(0.5, baseDelaySeconds)
        self.maxDelaySeconds = max(self.baseDelaySeconds, maxDelaySeconds)
    }

    public var canRetry: Bool {
        attempt < maxAttempts
    }

    /// True for retryable outcomes. `nil` status means transport failure.
    public static func shouldRetry(statusCode: Int?) -> Bool {
        guard let code = statusCode else { return true }
        if code == 408 || code == 429 { return true }
        if code >= 500 && code <= 599 { return true }
        return false
    }

    /// Backoff for the next retry: base * 2^attempt, capped at max.
    public func nextDelaySeconds() -> Double {
        let raw = baseDelaySeconds * pow(2.0, Double(attempt))
        return min(raw, maxDelaySeconds)
    }

    /// Copy with the attempt counter advanced by one.
    public func advanced() -> EvieRetryPlan {
        var copy = self
        copy.attempt += 1
        return copy
    }

    /// One line for diagnostics. Example: "retry 2/5 in 8s".
    public func describe() -> String {
        guard canRetry else { return "no more retries (\(attempt)/\(maxAttempts))" }
        let delay = Int(nextDelaySeconds().rounded())
        return "retry \(attempt + 1)/\(maxAttempts) in \(delay)s"
    }
}
