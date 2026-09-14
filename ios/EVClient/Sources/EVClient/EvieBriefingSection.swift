// Cycle EAC-39 — iPhone briefing section titles.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieBriefingSection: String, Sendable, CaseIterable {
    case objective, context, recommendation, talkingPoints

    public var title: String {
        switch self {
        case .objective: return "Objective"
        case .context: return "Context"
        case .recommendation: return "Recommendation"
        case .talkingPoints: return "Talking points"
        }
    }
}
