// Cycle EAC-60 — iPhone capture length guard.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieCaptureLength: Sendable {
    public static let maxChars = 4000

    public static func isOverLimit(_ text: String) -> Bool {
        text.count > maxChars
    }

    public static func remaining(_ text: String) -> Int {
        max(0, maxChars - text.count)
    }
}
