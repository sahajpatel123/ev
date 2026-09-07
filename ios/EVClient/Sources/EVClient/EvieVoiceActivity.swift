// Cycle EAC-12 — iPhone voice Live Activity island model.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Both iPhones show an active voice session in the Dynamic Island / Live
/// Activity area as compact text. This model is UI-framework-free (Foundation
/// only) so it compiles under CLT; the real ActivityKit presenter (Xcode-only)
/// consumes `islandLine()` verbatim. No backend change.

import Foundation

public struct EvieVoiceActivityState: Equatable, Sendable {
    public enum Phase: String, Sendable, Equatable {
        case idle
        case connecting
        case live
        case muted
        case ended
    }

    public var phase: Phase
    public var transcriptChars: Int
    public var hasApprovalHold: Bool

    public init(
        phase: Phase = .idle,
        transcriptChars: Int = 0,
        hasApprovalHold: Bool = false
    ) {
        self.phase = phase
        self.transcriptChars = max(0, transcriptChars)
        self.hasApprovalHold = hasApprovalHold
    }

    public var shouldShowIsland: Bool {
        phase == .connecting || phase == .live || phase == .muted
    }

    /// Compact Dynamic Island line. Examples: "EV live", "EV muted",
    /// "EV connecting…", "EV needs confirm".
    public func islandLine() -> String {
        if hasApprovalHold, shouldShowIsland {
            return "EV needs confirm"
        }
        switch phase {
        case .idle:
            return "EV idle"
        case .connecting:
            return "EV connecting…"
        case .live:
            return "EV live"
        case .muted:
            return "EV muted"
        case .ended:
            return "EV ended"
        }
    }

    public func accessibilityLabel() -> String {
        "Voice activity: \(islandLine())"
    }
}
