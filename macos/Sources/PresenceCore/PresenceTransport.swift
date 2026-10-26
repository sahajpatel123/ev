import Foundation

/// Connection state of a presence stream. Notes are human-readable and safe
/// to show on the HUD (never contain secrets or tokens).
public enum PresenceConnectionState: Sendable, Equatable {
    case offline(note: String)
    case connecting
    case live(note: String?)
    case reconnecting(note: String)
    case suspended(note: String)

    public var isLive: Bool {
        if case .live = self { return true }
        return false
    }

    public var note: String? {
        switch self {
        case .offline(let n), .reconnecting(let n), .suspended(let n): return n
        case .live(let n): return n
        case .connecting: return nil
        }
    }
}

/// One item from a presence transport: either a connection transition or a
/// contract event. A single stream keeps ordering trivial for every consumer.
public enum PresenceTransportEvent: Sendable, Equatable {
    case connection(PresenceConnectionState)
    case event(PresenceEvent)
}

/// Source of contract events. Implementations: the SSE client (D6, real
/// backend), the fixture replay (D6, scripted streams), later polls.
/// The HUD, confirm card, and timeline consume ONLY through this protocol.
public protocol PresenceTransport: Sendable {
    /// Yields `.connection` first, then `.event` frames until cancelled.
    /// Throwing ends the stream; the store surfaces the failure honestly.
    func run() -> AsyncThrowingStream<PresenceTransportEvent, Error>
    func stop()
}
