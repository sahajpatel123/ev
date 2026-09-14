// Cycle EAC-19 — iPhone haptic cue model.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Maps an Evie moment (routine note, timely nudge, urgent alert, approval
/// hold) to a named haptic pattern both iPhones (16 Pro + SE) can play with
/// the same Taptic Engine vocabulary. Names only — the Xcode-only presenter
/// translates `patternName()` to UIFeedbackGenerator calls, so CLT compiles
/// with Foundation alone. No backend change.

import Foundation

public struct EvieHapticCue: Equatable, Sendable {
    public enum Moment: String, Sendable, Equatable {
        case routine
        case timely
        case urgent
        case approvalHold
    }

    public var moment: Moment
    public var repeats: Int

    public init(moment: Moment = .routine, repeats: Int = 1) {
        self.moment = moment
        self.repeats = max(1, min(3, repeats))
    }

    /// Haptic pattern name the presenter plays. Urgent and approval-hold use
    /// the heaviest pattern so they feel distinct from routine notes.
    public func patternName() -> String {
        switch moment {
        case .routine:
            return "light"
        case .timely:
            return "medium"
        case .urgent:
            return "heavy"
        case .approvalHold:
            return "heavy"
        }
    }

    /// True when the cue should also play a sound alongside haptics.
    public var playsSound: Bool {
        moment == .urgent || moment == .approvalHold
    }

    /// One line for diagnostics. Example: "haptic heavy ×2 + sound".
    public func describe() -> String {
        var line = "haptic \(patternName())"
        if repeats > 1 {
            line += " ×\(repeats)"
        }
        if playsSound {
            line += " + sound"
        }
        return line
    }
}
