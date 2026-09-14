// Cycle EAC-16 — iPhone health readiness summary.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Formats already-fetched HealthKit numbers (steps, sleep, resting HR) into
/// the one-line readiness string both iPhones show on Today. Takes numbers,
/// not HealthKit objects, so the Xcode HealthKit manager stays the only
/// HealthKit importer and CLT still compiles. No backend change.

import Foundation

public struct EvieHealthSummary: Equatable, Sendable {
    public var steps: Int?
    public var sleepHours: Double?
    public var restingHeartRate: Double?

    public init(steps: Int? = nil, sleepHours: Double? = nil, restingHeartRate: Double? = nil) {
        self.steps = steps.map { max(0, $0) }
        self.sleepHours = sleepHours
        self.restingHeartRate = restingHeartRate
    }

    public enum Band: String, Sendable {
        case unknown
        case low
        case steady
        case primed
    }

    /// Heuristic band: sleep >= 7h and steps >= 6k reads as primed; low sleep
    /// or very low movement reads as low; anything partial is steady.
    public var band: Band {
        guard steps != nil || sleepHours != nil else { return .unknown }
        if let sleepHours, sleepHours < 5 { return .low }
        if let steps, steps < 1500, (sleepHours ?? 8) < 6 { return .low }
        if let sleepHours, sleepHours >= 7, let steps, steps >= 6000 { return .primed }
        return .steady
    }

    /// One line for Today. Examples: "8,200 steps · 7.5h sleep · steady".
    public func summaryLine() -> String {
        var parts: [String] = []
        if let steps {
            let fmt = NumberFormatter()
            fmt.numberStyle = .decimal
            let s = fmt.string(from: NSNumber(value: steps)) ?? "\(steps)"
            parts.append("\(s) steps")
        }
        if let sleepHours {
            parts.append(String(format: "%.1fh sleep", sleepHours))
        }
        if let restingHeartRate, restingHeartRate > 0 {
            parts.append(String(format: "RHR %.0f", restingHeartRate))
        }
        if parts.isEmpty { return "no health data yet" }
        parts.append(band.rawValue)
        return parts.joined(separator: " · ")
    }
}
