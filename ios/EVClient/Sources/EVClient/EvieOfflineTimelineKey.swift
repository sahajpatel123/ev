// Cycle EAC-22 — iPhone offline timeline cache key.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieOfflineTimelineKey: Equatable, Sendable {
    public var scope: String
    public var limit: Int

    public init(scope: String = "today", limit: Int = 50) {
        self.scope = scope
        self.limit = max(1, limit)
    }

    public var cacheKey: String {
        "timeline:\(scope):\(limit)"
    }
}
