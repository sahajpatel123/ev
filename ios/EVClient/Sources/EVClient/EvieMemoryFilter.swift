// Cycle EAC-25 — iPhone memory browser filter.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieMemoryFilter: Equatable, Sendable {
    public var query: String
    public var memoryType: String?

    public init(query: String = "", memoryType: String? = nil) {
        self.query = query
        self.memoryType = memoryType
    }

    public var isActive: Bool {
        !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || memoryType != nil
    }

    public func matches(type: String, text: String) -> Bool {
        if let memoryType, memoryType.lowercased() != type.lowercased() { return false }
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if q.isEmpty { return true }
        return text.lowercased().contains(q)
    }
}
