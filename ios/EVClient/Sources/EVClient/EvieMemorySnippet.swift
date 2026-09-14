// Cycle EAC-73 — iPhone memory snippet presentation.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Search results can be long and provenance can be absent.  This value type
/// keeps both facts visible on a small phone display without manufacturing a
/// score or source when the server did not provide one.
import Foundation

public struct EvieMemorySnippet: Equatable, Sendable, Identifiable {
    public let id: String
    public let memoryType: String
    public let text: String
    public let sourceType: String?
    public let isCurrent: Bool

    public init(
        id: String,
        memoryType: String,
        text: String,
        sourceType: String? = nil,
        isCurrent: Bool = true
    ) {
        self.id = id
        self.memoryType = memoryType
        self.text = text
        self.sourceType = sourceType
        self.isCurrent = isCurrent
    }

    public var displayTitle: String {
        let type = memoryType.trimmingCharacters(in: .whitespacesAndNewlines)
        return type.isEmpty ? "Memory" : type
    }

    public var provenanceLine: String {
        let source = sourceType?.trimmingCharacters(in: .whitespacesAndNewlines)
        let sourceLine = source?.isEmpty == false ? source! : "source not reported"
        return isCurrent ? sourceLine : "\(sourceLine) · historical version"
    }

    public func excerpt(query: String = "", limit: Int = 160) -> String {
        guard limit > 0 else { return "" }
        let cleaned = text
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        guard cleaned.count > limit else { return cleaned }

        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty,
              let match = cleaned.range(of: needle, options: [.caseInsensitive])
        else {
            return String(cleaned.prefix(max(0, limit - 1))) + "…"
        }

        let matchStart = cleaned.distance(from: cleaned.startIndex, to: match.lowerBound)
        let matchEnd = cleaned.distance(from: cleaned.startIndex, to: match.upperBound)
        let available = max(0, limit - needle.count - 2)
        let before = available / 2
        let after = available - before
        let startOffset = max(0, matchStart - before)
        let endOffset = min(cleaned.count, matchEnd + after)
        let start = cleaned.index(cleaned.startIndex, offsetBy: startOffset)
        let end = cleaned.index(cleaned.startIndex, offsetBy: endOffset)

        var result = String(cleaned[start..<end])
        if startOffset > 0 { result = "…" + result }
        if endOffset < cleaned.count { result += "…" }
        return String(result.prefix(limit))
    }
}
