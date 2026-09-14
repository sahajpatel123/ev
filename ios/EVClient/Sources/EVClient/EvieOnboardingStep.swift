// Cycle EAC-31 — iPhone onboarding checklist.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieOnboardingStep: Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var done: Bool

    public init(id: String, title: String, done: Bool = false) {
        self.id = id
        self.title = title
        self.done = done
    }

    public static func defaultSteps() -> [EvieOnboardingStep] {
        [
            EvieOnboardingStep(id: "grant", title: "Grant EVIE access"),
            EvieOnboardingStep(id: "voice", title: "Enroll your voice"),
            EvieOnboardingStep(id: "capture", title: "Capture one memory"),
        ]
    }

    public static func remaining(_ steps: [EvieOnboardingStep]) -> Int {
        steps.filter { !$0.done }.count
    }
}
