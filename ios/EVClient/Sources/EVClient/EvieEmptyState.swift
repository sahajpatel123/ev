// Cycle EAC-53 — iPhone empty-state copy catalog.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// One honest line per tab when there is nothing to show yet, identical on
/// iPhone 16 Pro and iPhone SE. Pure strings; the Xcode-only views read from
/// here. No backend change.

import Foundation

public enum EvieEmptyState: String, Sendable, CaseIterable {
    case today, chat, capture, memory, voice

    public var line: String {
        switch self {
        case .today: return "Nothing scheduled — enjoy the quiet"
        case .chat: return "Say hi — EV remembers from here"
        case .capture: return "Capture your first thought above"
        case .memory: return "No memories yet — they land here"
        case .voice: return "Tap Wake EV to start talking"
        }
    }
}
