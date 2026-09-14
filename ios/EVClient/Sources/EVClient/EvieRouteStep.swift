// Cycle EAC-41 — iPhone route step formatter.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieRouteStep: Equatable, Sendable {
    public var destination: String
    public var leaveBy: String?
    public var travelMinutes: Int?

    public init(destination: String, leaveBy: String? = nil, travelMinutes: Int? = nil) {
        self.destination = destination
        self.leaveBy = leaveBy
        self.travelMinutes = travelMinutes
    }

    public func displayLine() -> String {
        var parts = [destination]
        if let leaveBy { parts.append("leave \(leaveBy)") }
        if let travelMinutes { parts.append("\(travelMinutes) min") }
        return parts.joined(separator: " · ")
    }
}
