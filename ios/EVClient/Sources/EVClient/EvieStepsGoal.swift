// Cycle EAC-57 — iPhone steps goal progress.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieStepsGoal: Equatable, Sendable {
    public var steps: Int
    public var goal: Int

    public init(steps: Int = 0, goal: Int = 8000) {
        self.steps = max(0, steps)
        self.goal = max(1, goal)
    }

    public var fraction: Double {
        min(1, Double(steps) / Double(goal))
    }

    public func displayLine() -> String {
        "\(steps)/\(goal) steps"
    }
}
