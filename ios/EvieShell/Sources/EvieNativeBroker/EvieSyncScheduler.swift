import Foundation

/// iPhone-only: dynamic offline-sync scheduler.
///
/// Replaces fixed "retry N times" with per-kind priority, exponential backoff
/// with jitter ceiling, and a verdict (send now / wait / drop / quarantine)
/// the gateway queue can act on without hardcoding each case. Pure Foundation.
public struct EvieSyncItem: Equatable, Sendable {
    public var idempotencyKey: String
    public var kind: String
    public var attempts: Int
    public var lastError: String?

    public init(idempotencyKey: String, kind: String, attempts: Int = 0, lastError: String? = nil) {
        self.idempotencyKey = idempotencyKey
        self.kind = kind
        self.attempts = attempts
        self.lastError = lastError
    }
}

public enum EvieSyncVerdict: String, Equatable, Sendable {
    case sendNow
    case waitSeconds
    case drop
    case quarantine
}

public enum EvieSyncScheduler {
    public static let maxAttempts = 8

    /// Priority: user-visible captures first, telemetry last.
    public static func priority(for kind: String) -> Int {
        switch kind {
        case "siri_capture", "share_capture", "voice_capture": return 0
        case "file_sandbox", "reminder", "calendar": return 1
        case "heartbeat", "telemetry", "presence": return 2
        default: return 1
        }
    }

    /// Backoff: 2^attempts seconds capped at 300, never negative.
    public static func backoffSeconds(attempts: Int) -> Int {
        guard attempts > 0 else { return 0 }
        let shift = min(max(attempts - 1, 0), 8)
        return min(1 << shift, 300)
    }

    public static func verdict(for item: EvieSyncItem) -> (EvieSyncVerdict, Int) {
        if item.attempts >= maxAttempts { return (.quarantine, 0) }
        if item.lastError == "UNAUTHENTICATED" { return (.drop, 0) }
        let wait = backoffSeconds(attempts: item.attempts)
        if wait == 0 { return (.sendNow, 0) }
        return (.waitSeconds, wait)
    }

    /// Dynamic ordering: priority first, then fewest attempts, stable by key.
    public static func order(_ items: [EvieSyncItem]) -> [EvieSyncItem] {
        items.sorted {
            let p0 = priority(for: $0.kind), p1 = priority(for: $1.kind)
            if p0 != p1 { return p0 < p1 }
            if $0.attempts != $1.attempts { return $0.attempts < $1.attempts }
            return $0.idempotencyKey < $1.idempotencyKey
        }
    }
}
