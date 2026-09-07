// Cycle EAC-37 — iPhone sync summary line.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieSyncSummary: Equatable, Sendable {
    public var synced: Int
    public var dropped: Int
    public var quarantined: Int
    public var remaining: Int

    public init(synced: Int = 0, dropped: Int = 0, quarantined: Int = 0, remaining: Int = 0) {
        self.synced = max(0, synced)
        self.dropped = max(0, dropped)
        self.quarantined = max(0, quarantined)
        self.remaining = max(0, remaining)
    }

    public func statusLine() -> String {
        if remaining > 0 { return "\(remaining) waiting to sync" }
        if quarantined > 0 { return "\(quarantined) need review" }
        if synced > 0 { return "synced \(synced)" }
        return "everything synced"
    }
}
