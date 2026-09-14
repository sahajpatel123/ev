// Cycle EAC-51 — iPhone quiet-hours window.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Both iPhones (16 Pro + SE) hold routine notifications during the owner's
/// sleep window and release them as a morning digest instead of buzzing
/// overnight — matching the Mac runtime's quiet-hours digest. Urgent and
/// approval-hold moments always break through. Times only (no EventKit, no
/// UserDefaults) so CLT compiles with Foundation alone. No backend change.

import Foundation

public struct EvieQuietHours: Equatable, Sendable {
    /// Overnight window in 24h local hours, start-inclusive / end-exclusive.
    /// Default 22:00 → 07:00. A start equal to end means "never quiet".
    public var startHour: Int
    public var endHour: Int

    public init(startHour: Int = 22, endHour: Int = 7) {
        self.startHour = min(23, max(0, startHour))
        self.endHour = min(23, max(0, endHour))
    }

    public var isDisabled: Bool { startHour == endHour }

    /// True when `date` falls inside the quiet window (local time).
    public func isQuiet(at date: Date = Date()) -> Bool {
        if isDisabled { return false }
        let hour = Calendar.current.component(.hour, from: date)
        if startHour < endHour {
            return hour >= startHour && hour < endHour
        }
        return hour >= startHour || hour < endHour
    }

    /// True when a moment should buzz now even inside quiet hours.
    public static func breaksThrough(moment: String) -> Bool {
        moment == "urgent" || moment == "approvalHold"
    }

    /// One line for the inbox header. Example: "quiet 22:00–07:00".
    public func describe() -> String {
        if isDisabled { return "quiet hours off" }
        return String(format: "quiet %02d:00–%02d:00", startHour, endHour)
    }
}
