// Cycle EAC-75 — iPhone adaptive layout budget.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// These are presentation budgets, not hardware measurements.  The native
/// surface can use the same data model on the 16 Pro and SE while giving the
/// smaller display shorter labels and bounded previews.
import Foundation

public struct EvieCompactLayout: Equatable, Sendable {
    public enum Phone: String, Sendable, CaseIterable {
        case iPhone16Pro
        case iPhoneSE
    }

    public enum Surface: String, Sendable, CaseIterable {
        case today
        case conversation
        case capture
        case memory
        case voice
        case settings
    }

    public let phone: Phone

    public init(phone: Phone) {
        self.phone = phone
    }

    public var usesCompactLabels: Bool {
        phone == .iPhoneSE
    }

    public func lineLimit(for surface: Surface) -> Int {
        switch (phone, surface) {
        case (.iPhoneSE, .conversation), (.iPhoneSE, .memory):
            return 2
        case (.iPhoneSE, _):
            return 2
        case (.iPhone16Pro, .conversation), (.iPhone16Pro, .memory):
            return 4
        case (.iPhone16Pro, _):
            return 3
        }
    }

    public func previewCharacterLimit(for surface: Surface) -> Int {
        let base: Int
        switch surface {
        case .conversation:
            base = 240
        case .today, .voice:
            base = 180
        case .capture:
            base = 120
        case .memory:
            base = 160
        case .settings:
            base = 100
        }
        return usesCompactLabels ? max(80, base - 40) : base
    }

    public func preview(_ text: String, for surface: Surface) -> String {
        let cleaned = text
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        let limit = previewCharacterLimit(for: surface)
        guard cleaned.count > limit else { return cleaned }
        return String(cleaned.prefix(max(0, limit - 1))) + "…"
    }

    public var profileLine: String {
        phone == .iPhoneSE ? "iPhone SE · compact layout" : "iPhone 16 Pro · expanded layout"
    }
}
