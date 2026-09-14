// Cycle EAC-74 — iPhone conversation lease presentation.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Two phones may see the same conversation.  This model is deliberately a
/// presentation boundary: only a server-reported lease can be shown as
/// active, and taking over remains an explicit action for the caller.
import Foundation

public struct EvieConversationLease: Equatable, Sendable {
    public enum State: String, Sendable, CaseIterable {
        case available
        case heldByThisDevice = "held_by_this_device"
        case heldByOtherDevice = "held_by_other_device"
        case stale
        case moved
        case unknown
    }

    public enum Origin: String, Sendable {
        case server
        case localEstimate = "local_estimate"
    }

    public enum NextAction: String, Sendable {
        case start
        case continueHere = "continue_here"
        case takeOver = "take_over"
        case reconnect
        case wait
    }

    public let state: State
    public let holderLabel: String?
    public let origin: Origin

    public init(
        state: State,
        holderLabel: String? = nil,
        origin: Origin
    ) {
        self.state = state
        self.holderLabel = holderLabel?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.origin = origin
    }

    public var isServerReported: Bool {
        origin == .server
    }

    public var isActiveHere: Bool {
        isServerReported && state == .heldByThisDevice
    }

    public var canOfferTakeover: Bool {
        isServerReported && state == .heldByOtherDevice
    }

    public var nextAction: NextAction {
        guard isServerReported else { return .reconnect }
        switch state {
        case .available, .stale:
            return .start
        case .heldByThisDevice:
            return .continueHere
        case .heldByOtherDevice:
            return .takeOver
        case .moved, .unknown:
            return .reconnect
        }
    }

    public var displayLine: String {
        guard isServerReported else { return "Checking conversation status…" }
        switch state {
        case .available:
            return "Conversation available"
        case .heldByThisDevice:
            return "Conversation active here"
        case .heldByOtherDevice:
            let holder = holderLabel?.isEmpty == false ? holderLabel! : "another device"
            return "Conversation active on \(holder)"
        case .stale:
            return "Previous conversation lease expired"
        case .moved:
            return "Conversation moved to another device"
        case .unknown:
            return "Conversation status unavailable"
        }
    }
}
