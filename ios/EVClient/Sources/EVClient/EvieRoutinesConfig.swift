/// iPhone-only (EAC98): gateway routines payload + next-digest math.
///
/// Decodes GET/PUT /v1/device-gateway/routines and answers "when is the
/// next digest?" in the phone's own timezone — pure Foundation, no I/O.

import Foundation

public struct EvieRoutinesConfig: Codable, Equatable, Sendable {
    public var enabled: Bool
    public var digestTimes: [String]
    public var quietHoursStart: String?
    public var quietHoursEnd: String?
    public var timezone: String

    public init(
        enabled: Bool,
        digestTimes: [String],
        quietHoursStart: String?,
        quietHoursEnd: String?,
        timezone: String
    ) {
        self.enabled = enabled
        self.digestTimes = digestTimes
        self.quietHoursStart = quietHoursStart
        self.quietHoursEnd = quietHoursEnd
        self.timezone = timezone
    }

    private enum CodingKeys: String, CodingKey {
        case enabled
        case digestTimes = "digest_times"
        case quietHoursStart = "quiet_hours_start"
        case quietHoursEnd = "quiet_hours_end"
        case timezone
    }

    /// Next digest fire time at/after `now` in the configured timezone,
    /// respecting quiet hours; nil when disabled or no times set.
    public func nextDigest(after now: Date = Date()) -> Date? {
        guard enabled, !digestTimes.isEmpty else { return nil }
        let tz = TimeZone(identifier: timezone) ?? TimeZone.current
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = tz
        let minutes = digestTimes.compactMap { Self.minutesOfDay($0) }.sorted()
        guard !minutes.isEmpty else { return nil }
        var candidate = calendar.startOfDay(for: now)
        for dayOffset in 0...2 {
            for minute in minutes {
                guard let fire = calendar.date(byAdding: .minute, value: minute, to: calendar.date(byAdding: .day, value: dayOffset, to: candidate) ?? candidate) else { continue }
                if fire >= now && !isInsideQuietHours(fire, calendar: calendar) {
                    return fire
                }
            }
        }
        return nil
    }

    public func isInsideQuietHours(_ date: Date, calendar: Calendar? = nil) -> Bool {
        guard let start = Self.minutesOfDay(quietHoursStart ?? ""),
              let end = Self.minutesOfDay(quietHoursEnd ?? "")
        else { return false }
        let cal = calendar ?? {
            var c = Calendar(identifier: .gregorian)
            c.timeZone = TimeZone(identifier: timezone) ?? TimeZone.current
            return c
        }()
        let comps = cal.dateComponents([.hour, .minute], from: date)
        let minute = (comps.hour ?? 0) * 60 + (comps.minute ?? 0)
        if start < end {
            return minute >= start && minute < end
        }
        return minute >= start || minute < end
    }

    static func minutesOfDay(_ hhmm: String) -> Int? {
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        guard parts.count == 2, parts[0] >= 0, parts[0] <= 23, parts[1] >= 0, parts[1] <= 59 else { return nil }
        return parts[0] * 60 + parts[1]
    }
}
