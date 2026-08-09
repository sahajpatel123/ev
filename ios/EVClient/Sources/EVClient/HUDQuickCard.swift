/// Tactical quick card (`ev.hud.quickcard.v1`) for <800 ms HUD reads.
///
/// Same contract on every surface: the compact text render mirrors the CLI
/// `ev quickcard` output and the web workbench quick-card panel.

import Foundation

public struct HUDQuickCard: Codable, Sendable, Equatable {
    public static let schemaVersionV1 = "ev.hud.quickcard.v1"

    public let schemaVersion: String
    public let generatedAt: String
    public let objective: String
    public let summary: String
    public let nextAction: String?
    public let topRisk: String?
    public let peopleCount: Int
    public let optionsCount: Int
    public let decisionHistoryCount: Int
    public let meta: [String: AnyCodable]?

    public init(
        schemaVersion: String = HUDQuickCard.schemaVersionV1,
        generatedAt: String,
        objective: String,
        summary: String,
        nextAction: String? = nil,
        topRisk: String? = nil,
        peopleCount: Int = 0,
        optionsCount: Int = 0,
        decisionHistoryCount: Int = 0,
        meta: [String: AnyCodable]? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.generatedAt = generatedAt
        self.objective = objective
        self.summary = summary
        self.nextAction = nextAction
        self.topRisk = topRisk
        self.peopleCount = peopleCount
        self.optionsCount = optionsCount
        self.decisionHistoryCount = decisionHistoryCount
        self.meta = meta
    }

    public func validate() throws {
        guard schemaVersion == Self.schemaVersionV1 else {
            throw HUDCardError.unsupportedSchema(schemaVersion)
        }
    }

    public func renderText() -> String {
        var metaParts: [String] = []
        if let nextAction {
            metaParts.append("next: \(nextAction)")
        }
        if let topRisk {
            metaParts.append("risk: \(topRisk)")
        }
        metaParts.append(
            "people \(peopleCount) · options \(optionsCount) · history \(decisionHistoryCount)"
        )
        return [
            "[\(schemaVersion)] \(objective)",
            summary,
            metaParts.joined(separator: " | "),
        ].joined(separator: "\n")
    }
}
