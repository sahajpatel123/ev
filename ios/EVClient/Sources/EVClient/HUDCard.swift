/// HUD card rendering for watch/widget/AR surfaces (`ev.hud.card.v1`).
///
/// One decoded card renders the same way on every surface; the compact text
/// render mirrors the CLI and web workbench output.

import Foundation

public enum HUDCardError: Error, Equatable, Sendable {
    case unsupportedSchema(String)
}

public struct LookoutUtteranceResult: Codable, Sendable {
    public let reply: String
    public let conversationId: String?
    public let preferHaptic: Bool?
    public let hud: HUDCard?

    public init(
        reply: String,
        conversationId: String? = nil,
        preferHaptic: Bool? = true,
        hud: HUDCard? = nil
    ) {
        self.reply = reply
        self.conversationId = conversationId
        self.preferHaptic = preferHaptic
        self.hud = hud
    }
}

public struct HUDCard: Codable, Sendable, Equatable {
    public static let schemaVersionV1 = "ev.hud.card.v1"

    public let schemaVersion: String
    public let generatedAt: String
    public let title: String
    public let body: String
    public let priority: Double
    public let meta: [String: AnyCodable]?

    public init(
        schemaVersion: String = HUDCard.schemaVersionV1,
        generatedAt: String,
        title: String,
        body: String,
        priority: Double = 0.0,
        meta: [String: AnyCodable]? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.generatedAt = generatedAt
        self.title = title
        self.body = body
        self.priority = priority
        self.meta = meta
    }

    public func validate() throws {
        guard schemaVersion == Self.schemaVersionV1 else {
            throw HUDCardError.unsupportedSchema(schemaVersion)
        }
    }

    /// Compact render shared by Watch complications, widgets, and voice
    /// one-liners: `[schema] title (priority N)` + body.
    public func renderText() -> String {
        "[\(schemaVersion)] \(title) (priority \(priority))\n\(body)"
    }
}
