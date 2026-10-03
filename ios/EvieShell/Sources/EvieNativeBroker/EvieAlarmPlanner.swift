import Foundation

/// iPhone-only: dynamic alarm/timer schedule planner.
///
/// iOS exposes no public alarm-clock API, so this planner answers honestly
/// what the phone CAN do for any requested wake-up: a one-shot countdown
/// notification, a wall-clock notification for "7am tomorrow", or a repeat
/// daily schedule — plus what it can NOT do (a real Clock.app alarm). The
/// broker reports `evie_notification` as the timer kind, never "alarm".
/// Pure Foundation so the package build verifies it.
public enum EvieAlarmSchedule: Equatable, Sendable {
    case countdown(seconds: Int)
    case wallClock(fireDate: Date)
    case unsupported(reason: String)
}

public struct EvieAlarmPlan: Equatable, Sendable {
    public var schedule: EvieAlarmSchedule
    public var label: String
    public var timerKind: String
    public var repeats: Bool

    public init(schedule: EvieAlarmSchedule, label: String, timerKind: String = "evie_notification", repeats: Bool = false) {
        self.schedule = schedule
        self.label = label
        self.timerKind = timerKind
        self.repeats = repeats
    }
}

public enum EvieAlarmPlanner {
    public static func plan(
        label: String?,
        delaySeconds: Int?,
        fireDate: Date?,
        repeats: Bool = false,
        now: Date = Date()
    ) -> EvieAlarmPlan {
        let title = label?.trimmingCharacters(in: .whitespacesAndNewlines)
        let name = (title?.isEmpty == false) ? title! : "Evie timer"
        if let fireDate, fireDate > now {
            return .init(schedule: .wallClock(fireDate: fireDate), label: name, repeats: repeats)
        }
        if let delaySeconds, delaySeconds > 0 {
            return .init(schedule: .countdown(seconds: delaySeconds), label: name, repeats: false)
        }
        if let fireDate {
            _ = fireDate
            return .init(schedule: .unsupported(reason: "fire_date_in_past"), label: name)
        }
        return .init(schedule: .unsupported(reason: "no_time_specified"), label: name)
    }

    /// Dynamic ISO-8601 parse used by the broker for `when_iso` payloads.
    public static func parseFireDate(_ iso: String?) -> Date? {
        guard let iso, !iso.isEmpty else { return nil }
        return ISO8601DateFormatter().date(from: iso)
    }
}
