// Cycle EAC-33 — iPhone message draft validator.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieMessageDraft: Equatable, Sendable {
    public var recipients: [String]
    public var body: String

    public init(recipients: [String] = [], body: String = "") {
        self.recipients = recipients
        self.body = body
    }

    public var canSend: Bool {
        !recipients.isEmpty && !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
