// Cycle EAC-54 — iPhone memory search normalizer.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieSearchNormalizer: Sendable {
    public static func normalize(_ raw: String) -> String {
        raw.lowercased()
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }
}
