// Cycle EAC-24 — Watch quick-capture draft.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieWatchQuickCapture: Equatable, Sendable {
    public var text: String

    public init(text: String = "") {
        self.text = text
    }

    public var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public func captureText() -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
