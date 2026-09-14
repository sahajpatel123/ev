// Cycle EAC-67 — iPhone action receipt state.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// A receipt is presentation state, not permission to perform an action.  The
/// server remains authoritative; the phone can only display a success claim
/// when the returned status is `executed` and verification is explicit.
import Foundation

public struct EvieActionReceipt: Equatable, Sendable {
    public enum Status: String, Sendable, CaseIterable {
        case accepted
        case queued
        case executing
        case executed
        case failed
        case denied
    }

    public let id: String
    public let operation: String
    public let status: Status
    public let verified: Bool
    public let message: String?
    public let errorCode: String?

    public init(
        id: String,
        operation: String,
        status: Status,
        verified: Bool = false,
        message: String? = nil,
        errorCode: String? = nil
    ) {
        self.id = id
        self.operation = operation
        self.status = status
        self.verified = verified
        self.message = message
        self.errorCode = errorCode
    }

    /// Only a server-reported execution with explicit verification is a
    /// success that the phone may render as complete.
    public var isVerifiedSuccess: Bool {
        status == .executed && verified
    }

    public var isTerminal: Bool {
        switch status {
        case .executed, .failed, .denied:
            return true
        case .accepted, .queued, .executing:
            return false
        }
    }

    /// A failed operation may be offered again; denial generally requires a
    /// fresh owner decision and is therefore not silently retryable.
    public var canRetry: Bool {
        status == .failed
    }

    public var displayLine: String {
        let name = operation.trimmingCharacters(in: .whitespacesAndNewlines)
        let label = name.isEmpty ? "Action" : name
        switch status {
        case .accepted:
            return "(label) · accepted"
        case .queued:
            return "(label) · queued"
        case .executing:
            return "(label) · working"
        case .executed:
            return isVerifiedSuccess
                ? "(label) · completed · verified"
                : "(label) · completed · verification pending"
        case .failed:
            return "(label) · failed"
        case .denied:
            return "(label) · denied"
        }
    }
}
