// Cycle EAC-35 — iPhone contact match display.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieContactMatch: Equatable, Sendable, Identifiable {
    public var id: String
    public var displayName: String
    public var phone: String?

    public init(id: String = UUID().uuidString, displayName: String, phone: String? = nil) {
        self.id = id
        self.displayName = displayName
        self.phone = phone
    }

    public func displayLine() -> String {
        if let phone, !phone.isEmpty { return "\(displayName) · \(phone)" }
        return displayName
    }
}
