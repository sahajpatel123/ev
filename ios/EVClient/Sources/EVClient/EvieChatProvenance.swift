// Cycle EAC-11 — iPhone chat provenance badge model.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Both iPhones (16 Pro + SE) show chat SSE side-channels
/// (`memory-delta` / `provenance` / `filter-report` / `refined`) as one
/// compact badge line under the reply, so "just chat" becomes visibly
/// grounded without any backend change.

import Foundation

public struct EvieChatProvenance: Equatable, Sendable {
    public var memoryDeltas: Int
    public var provenanceCount: Int
    public var filterFlags: Int
    public var hasRefined: Bool

    public init(
        memoryDeltas: Int = 0,
        provenanceCount: Int = 0,
        filterFlags: Int = 0,
        hasRefined: Bool = false
    ) {
        self.memoryDeltas = max(0, memoryDeltas)
        self.provenanceCount = max(0, provenanceCount)
        self.filterFlags = max(0, filterFlags)
        self.hasRefined = hasRefined
    }

    public var hasGroundedMemory: Bool {
        memoryDeltas > 0 || provenanceCount > 0
    }

    public var hasFilterNote: Bool {
        filterFlags > 0
    }

    /// One-line badge for the iPhone chat bubble footer.
    /// Examples: "3 memories · refined", "2 sources", "no saved memory yet".
    public func summaryLine() -> String {
        var parts: [String] = []
        if memoryDeltas > 0 {
            parts.append(memoryDeltas == 1 ? "1 memory" : "\(memoryDeltas) memories")
        }
        if provenanceCount > 0 {
            parts.append(provenanceCount == 1 ? "1 source" : "\(provenanceCount) sources")
        }
        if hasRefined {
            parts.append("refined")
        }
        if hasFilterNote {
            parts.append(filterFlags == 1 ? "1 filter note" : "\(filterFlags) filter notes")
        }
        if parts.isEmpty {
            return "no saved memory yet"
        }
        return parts.joined(separator: " · ")
    }

    public func accessibilityLabel() -> String {
        "Chat provenance: \(summaryLine())"
    }
}
