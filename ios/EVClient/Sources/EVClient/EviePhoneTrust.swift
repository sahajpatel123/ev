// Cycle EAC-77 — iPhone trust-state presentation.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Trust is established by Home Station.  A phone-side estimate may inform a
/// loading state, but it can never authorize sensitive work or render itself
/// as an owner-trusted device.
import Foundation

public struct EviePhoneTrust: Equatable, Sendable {
    public enum State: String, Sendable, CaseIterable {
        case pairedSandbox = "paired_sandbox"
        case paired
        case owner
        case revoked
        case unknown
    }

    public enum Source: String, Sendable {
        case server
        case localEstimate = "local_estimate"
    }

    public enum Capability: String, Sendable, CaseIterable {
        case readOnly = "read_only"
        case capture
        case liveVoice = "live_voice"
        case camera
        case sensitiveAction = "sensitive_action"
    }

    public let state: State
    public let source: Source

    public init(state: State, source: Source) {
        self.state = state
        self.source = source
    }

    public var isServerReported: Bool {
        source == .server
    }

    public var isOwnerTrusted: Bool {
        isServerReported && state == .owner
    }

    public func allows(_ capability: Capability) -> Bool {
        guard isServerReported else {
            return capability == .readOnly && state != .revoked
        }
        switch state {
        case .owner:
            return true
        case .paired:
            return capability == .readOnly
        case .pairedSandbox, .revoked, .unknown:
            return false
        }
    }

    public var displayLine: String {
        guard isServerReported else { return "Checking phone trust…" }
        switch state {
        case .pairedSandbox:
            return "Paired sandbox · promotion required"
        case .paired:
            return "Paired phone · read-only"
        case .owner:
            return "Owner-trusted phone"
        case .revoked:
            return "Phone trust revoked"
        case .unknown:
            return "Phone trust unavailable"
        }
    }
}
