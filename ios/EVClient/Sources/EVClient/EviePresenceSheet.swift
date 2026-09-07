// Cycle EAC-20 — iPhone presence sheet mapping.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// iOS shows the same `ev://present` kinds as sheets summoned by EVIE (never a
/// tab app). This maps each macOS PresenceKind (docs/APPLE_CLIENTS.md §15) to
/// the iOS sheet detent + auto-dismiss behavior both iPhones (16 Pro + SE)
/// use. Names only — the Xcode-only presenter translates them to SwiftUI
/// `.sheet` + `.presentationDetents`, so CLT compiles with Foundation alone.
/// No backend change.

import Foundation

public struct EviePresenceSheet: Equatable, Sendable {
    public enum Kind: String, Sendable, Equatable, CaseIterable {
        case card
        case briefing
        case list
        case conversation
        case map
        case chip
        case radar
        case vitals
        case horizon
        case scope
        case bench
        case trace
        case pulse
        case ticker
        case wire
    }

    public var kind: Kind

    public init(kind: Kind = .card) {
        self.kind = kind
    }

    /// Sheet detent name the presenter uses.
    public func detentName() -> String {
        switch kind {
        case .chip, .pulse:
            return "compact"
        case .card, .briefing, .conversation, .radar, .vitals, .horizon, .scope, .bench:
            return "medium"
        case .list, .map, .trace, .ticker, .wire:
            return "large"
        }
    }

    /// Auto-dismiss seconds for timed kinds; nil means stays until dismissed.
    public func autoDismissSeconds() -> Double? {
        switch kind {
        case .chip, .pulse:
            return 1.6
        case .ticker:
            return 5
        case .card, .briefing:
            return 30
        case .list, .conversation, .map, .radar, .vitals, .horizon, .scope, .bench, .trace, .wire:
            return nil
        }
    }

    /// True for corner-lookout kinds that stack instead of replacing the sheet.
    public var showsAsLookout: Bool {
        switch kind {
        case .radar, .vitals, .horizon, .scope, .bench:
            return true
        default:
            return false
        }
    }
}
