// Cycle EAC-58 — iPhone voice consent state.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieVoiceConsent: Equatable, Sendable {
    public var granted: Bool
    public var enrolled: Bool

    public init(granted: Bool = false, enrolled: Bool = false) {
        self.granted = granted
        self.enrolled = enrolled
    }

    public var canTalk: Bool { granted && enrolled }

    public func nextStep() -> String {
        if !granted { return "Grant microphone access first" }
        if !enrolled { return "Enroll your voiceprint first" }
        return "Ready to talk"
    }
}
