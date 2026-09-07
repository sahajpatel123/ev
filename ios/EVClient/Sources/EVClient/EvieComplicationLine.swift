// Cycle EAC-50 — Watch complication line truncator.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieComplicationLine: Sendable {
    public static func twoLines(headline: String, body: String) -> [String] {
        let h = headline.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = body.components(separatedBy: "|").map {
            $0.trimmingCharacters(in: .whitespacesAndNewlines)
        }.filter { !$0.isEmpty }
        if h.isEmpty { return Array(parts.prefix(2)) }
        let second = parts.first ?? ""
        return second.isEmpty ? [h] : [h, String(second.prefix(60))]
    }
}
