// Cycle EAC-79 — iPhone Look result receipt.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Camera results may be empty, degraded, remotely processed, or retained.
/// The phone keeps those facts visible instead of turning an unavailable or
/// low-quality result into a confident answer.
import Foundation

public struct EvieLookReceipt: Equatable, Sendable {
    public enum Status: String, Sendable, CaseIterable {
        case requested
        case captured
        case analyzed
        case saved
        case denied
        case unavailable
        case failed
    }

    public let status: Status
    public let cameraLabel: String
    public let answer: String
    public let sentToModel: Bool?
    public let rawMediaRetained: Bool?
    public let degraded: Bool

    public init(
        status: Status,
        cameraLabel: String = "",
        answer: String = "",
        sentToModel: Bool? = nil,
        rawMediaRetained: Bool? = nil,
        degraded: Bool = false
    ) {
        self.status = status
        self.cameraLabel = cameraLabel.trimmingCharacters(in: .whitespacesAndNewlines)
        self.answer = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        self.sentToModel = sentToModel
        self.rawMediaRetained = rawMediaRetained
        self.degraded = degraded
    }

    public var hasAnswer: Bool {
        (status == .analyzed || status == .saved) && !answer.isEmpty
    }

    public var displayLine: String {
        let camera = cameraLabel.isEmpty ? "Camera" : cameraLabel
        switch status {
        case .requested:
            return "Look requested on \(camera)"
        case .captured:
            return "Look captured on \(camera)"
        case .analyzed, .saved:
            return hasAnswer ? "Look · \(answer)" : "Look completed · no readable result"
        case .denied:
            return "Look needs camera access"
        case .unavailable:
            return "Look is unavailable"
        case .failed:
            return "Look failed"
        }
    }

    public var privacyLine: String {
        let processing: String
        switch sentToModel {
        case true: processing = "sent to model"
        case false: processing = "not sent to model"
        case nil: processing = "model destination unknown"
        }
        let retention: String
        switch rawMediaRetained {
        case true: retention = "raw media retained"
        case false: retention = "raw media not retained"
        case nil: retention = "raw-media retention unknown"
        }
        let quality = degraded ? " · degraded result" : ""
        return "\(processing) · \(retention)\(quality)"
    }
}
