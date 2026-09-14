// Cycle EAC-78 — iPhone notification delivery receipt.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// A notification row is not proof that APNs delivered anything.  This model
/// keeps the transport outcome explicit so the phone can show polling or a
/// registration gap instead of a false push-success badge.
import Foundation

public struct EvieNotificationDelivery: Equatable, Sendable {
    public enum Channel: String, Sendable, CaseIterable {
        case push
        case polling
        case local
        case unknown
    }

    public enum State: String, Sendable, CaseIterable {
        case delivered
        case queued
        case notRegistered = "not_registered"
        case disabled
        case failed
        case unknown
    }

    public let channel: Channel
    public let state: State
    public let reason: String?

    public init(
        channel: Channel,
        state: State,
        reason: String? = nil
    ) {
        self.channel = channel
        self.state = state
        self.reason = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var isConfirmedDelivery: Bool {
        state == .delivered
    }

    public var needsAttention: Bool {
        switch state {
        case .notRegistered, .disabled, .failed, .unknown:
            return true
        case .delivered, .queued:
            return false
        }
    }

    public var displayLine: String {
        switch state {
        case .delivered:
            return channel == .push ? "Push delivered" : "Notification delivered via \(channel.rawValue)"
        case .queued:
            return "Notification queued"
        case .notRegistered:
            return "Push registration required"
        case .disabled:
            return "Push delivery disabled"
        case .failed:
            return "Notification delivery failed"
        case .unknown:
            return "Notification delivery unknown"
        }
    }

    public var detailLine: String? {
        guard let reason, !reason.isEmpty else { return nil }
        return reason
    }
}
