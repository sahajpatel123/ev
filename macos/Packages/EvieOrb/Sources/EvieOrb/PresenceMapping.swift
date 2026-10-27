import Foundation

/// Maps `evie-presence/1` event names (§3 of PRESENCE_EVENTS.md on the
/// `evie-presence` branch) to orb states. One table, documented:
///
/// | presence event     | orb state    | note                                  |
/// | ------------------ | ------------ | ------------------------------------- |
/// | `turn.listening`   | `.listening` | mic open; energy from input level     |
/// | `turn.thinking`    | `.thinking`  | brain reasoning                       |
/// | `tool.started`     | `.acting`    | orbit highlight per active step       |
/// | `turn.needs_confirm`| `.needsConfirm` | calm pulse + accent tint         |
/// | `turn.completed`   | `.success`   | one soft bloom, then idle             |
/// | `turn.failed`      | `.error`     | brief cool dim, then idle             |
/// | `panic.state`      | `.error` / `.idle` | held dim while panicked    |
/// | *(none: local)*    | `.offline`   | stream silent past timeout (below)    |
///
/// Events not in this table (transcripts, plan steps, progress lines, future
/// additions) return `nil`: they drive CLI 2's content views, not the orb.
/// `turn.progress` with `spoken=true` does NOT force `.speaking` — the
/// speaking state follows real output audio; when TTS starts, the app sets
/// `.speaking` directly and the envelope breathes from the playback tap.
public enum PresenceMapping {
    public static func orbState(for eventName: String, panicked: Bool = false) -> OrbState? {
        switch eventName {
        case "turn.listening":
            return .listening
        case "turn.thinking":
            return .thinking
        case "tool.started":
            return .acting
        case "turn.needs_confirm":
            return .needsConfirm
        case "turn.completed":
            return .success
        case "turn.failed":
            return .error
        case "panic.state":
            return panicked ? .error : .idle
        default:
            return nil
        }
    }
}

/// Derives `.offline` locally: the presence contract has no offline event
/// (heartbeats every ≤15 s prove liveness instead), so silence past the
/// timeout means the stream is down. Feed every received frame or heartbeat.
public struct OfflineDetector: Sendable {
    public var timeout: TimeInterval
    private var lastFrameAt: TimeInterval? = nil

    public init(timeout: TimeInterval = 20) {
        self.timeout = timeout
    }

    public mutating func noteFrame(at now: TimeInterval) {
        lastFrameAt = now
    }

    public func isOffline(now: TimeInterval) -> Bool {
        guard let last = lastFrameAt else { return true }
        return now - last > timeout
    }
}
