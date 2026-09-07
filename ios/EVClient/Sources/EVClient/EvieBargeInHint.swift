// Cycle EAC-44 — iPhone barge-in hint.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieBargeInHint: Equatable, Sendable {
    public var canInterrupt: Bool

    public init(canInterrupt: Bool = false) {
        self.canInterrupt = canInterrupt
    }

    public func displayLine() -> String {
        canInterrupt ? "speak anytime to interrupt" : "let EV finish first"
    }
}
