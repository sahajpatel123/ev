// Cycle EAC-40 — iPhone focus lock display.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieFocusState: Equatable, Sendable {
    public var focus: String?
    public var locked: Bool

    public init(focus: String? = nil, locked: Bool = false) {
        self.focus = focus
        self.locked = locked
    }

    public func displayLine() -> String {
        guard let focus, !focus.isEmpty else { return "focus open" }
        return locked ? "locked: \(focus)" : "focus: \(focus)"
    }
}
