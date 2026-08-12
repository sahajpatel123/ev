/// HUD briefing / focus / route schemas (`ev.hud.briefing.v1`,
/// `ev.hud.focus.v1`, `ev.hud.route.v1`) with compact text renderers shared
/// by Watch/widget/web surfaces.

import Foundation

public struct HUDBriefing: Codable, Sendable, Equatable {
    public static let schemaVersionV1 = "ev.hud.briefing.v1"

    public let schemaVersion: String
    public let generatedAt: String
    public let objective: String
    public let context: String
    public let people: [AnyCodable]?
    public let risks: [AnyCodable]?
    public let options: [AnyCodable]?
    public let recommendation: String?
    public let decisionHistory: [AnyCodable]?
    public let talkingPoints: [String]?
    public let provenance: [AnyCodable]?

    public func validate() throws {
        guard schemaVersion == Self.schemaVersionV1 else {
            throw HUDCardError.unsupportedSchema(schemaVersion)
        }
    }

    public func renderText() -> String {
        var lines = ["[\(schemaVersion)] \(objective)", context]
        if let recommendation {
            lines.append("recommendation: \(recommendation)")
        }
        if let talkingPoints, !talkingPoints.isEmpty {
            lines.append("talking points: \(talkingPoints.joined(separator: " · "))")
        }
        return lines.joined(separator: "\n")
    }
}

public struct HUDFocus: Codable, Sendable, Equatable {
    public static let schemaVersionV1 = "ev.hud.focus.v1"

    public let schemaVersion: String
    public let generatedAt: String
    public let focus: AnyCodable?
    public let locked: Bool
    public let context: String
    public let nextAction: String?
    public let meta: [String: AnyCodable]?

    public func validate() throws {
        guard schemaVersion == Self.schemaVersionV1 else {
            throw HUDCardError.unsupportedSchema(schemaVersion)
        }
    }

    public func renderText() -> String {
        var lines = ["[\(schemaVersion)] focus \(locked ? "locked" : "open")", context]
        if let nextAction {
            lines.append("next: \(nextAction)")
        }
        return lines.joined(separator: "\n")
    }
}

public struct HUDRoute: Codable, Sendable, Equatable {
    public static let schemaVersionV1 = "ev.hud.route.v1"

    public let schemaVersion: String
    public let generatedAt: String
    public let destination: String?
    public let leaveBy: String?
    public let travelTimeMinutes: Int?
    public let prepChecklist: [String]?
    public let notes: [String]?

    public func validate() throws {
        guard schemaVersion == Self.schemaVersionV1 else {
            throw HUDCardError.unsupportedSchema(schemaVersion)
        }
    }

    public func renderText() -> String {
        var lines = [
            "[\(schemaVersion)] \(destination ?? "route")",
            "leave by \(leaveBy ?? "?") · travel \(travelTimeMinutes.map(String.init) ?? "?") min",
        ]
        if let prepChecklist, !prepChecklist.isEmpty {
            lines.append("prep: \(prepChecklist.joined(separator: " · "))")
        }
        return lines.joined(separator: "\n")
    }
}
