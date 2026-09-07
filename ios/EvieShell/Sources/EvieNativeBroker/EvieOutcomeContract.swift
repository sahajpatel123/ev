import Foundation

/// iPhone-only (EAC79): outcome vocabulary shared with the gateway.
///
/// The server reports phone-action outcomes with one of four statuses:
/// FAILED / QUEUED / ACCEPTED / COMPLETED (see phone_mac._action_status).
/// The native shell must render the same vocabulary and must never claim
/// an outcome it cannot prove: executed => accepted, verified => executed.
/// Pure Foundation — compile-checked on macOS.
public enum EvieOutcomeStatus: String, Equatable, Sendable, CaseIterable {
    case failed = "FAILED"
    case queued = "QUEUED"
    case accepted = "ACCEPTED"
    case completed = "COMPLETED"
}

public struct EvieOutcomeState: Equatable, Sendable {
    public let accepted: Bool
    public let executed: Bool
    public let verified: Bool
    public let queued: Bool
    public let errorCode: String?

    public init(
        accepted: Bool,
        executed: Bool,
        verified: Bool,
        queued: Bool = false,
        errorCode: String? = nil
    ) {
        self.accepted = accepted
        self.executed = executed
        self.verified = verified
        self.queued = queued
        self.errorCode = errorCode
    }

    /// Server vocabulary. An executed action that Apple only presented via
    /// system UI is reported ACCEPTED unless verified by a receipt.
    public var status: EvieOutcomeStatus {
        if !accepted {
            return .failed
        }
        if queued && !executed {
            return .queued
        }
        if executed {
            return .completed
        }
        return .accepted
    }

    /// A claim is honest only when every stronger claim is backed.
    public var isHonest: Bool {
        if executed && !accepted { return false }
        if verified && !executed { return false }
        return true
    }
}

public enum EvieOutcomeContract {
    /// Server error codes known to the phone surfaces.
    public static let errorCopy: [String: String] = [
        "AMBIGUOUS": "Multiple matches — tell me which one.",
        "NOT_FOUND": "I couldn't find that.",
        "MISSING_MESSAGE_FIELDS": "I need both the recipient and the text.",
        "WEATHER_TIMEOUT": "Weather lookup timed out.",
        "WEATHER_UNAVAILABLE": "Weather is unavailable right now.",
        "capture_requires_owner": "This phone needs Mac approval first.",
        "not_connected": "That app isn't connected yet.",
        "lease_not_held": "Another device is speaking.",
    ]

    public static func describe(status: EvieOutcomeStatus, errorCode: String?) -> String {
        switch status {
        case .failed:
            if let code = errorCode, let copy = errorCopy[code] {
                return copy
            }
            return "That didn't go through."
        case .queued:
            return "Queued — it will run on Home Station."
        case .accepted:
            return "Accepted — check the system prompt to finish it."
        case .completed:
            return "Done."
        }
    }
}
