import Foundation

// Follow-Me (cross-device intent bus) client models + EVAPIClient extension.
// Talks to the additive Follow-Me endpoints under /v1/everywhere.

public struct EvieIntentReceipt: Codable, Sendable, Equatable {
    public let device_id: String?
    public let status: String
    public let note: String
    public let at: String
}

public struct EvieIntent: Codable, Sendable, Equatable {
    public let id: String
    public let kind: String
    public let capability: String
    public let args: [String: AnyCodableValue]
    public let source_device_id: String?
    public let target_device_id: String?
    public let status: String
    public let created_at: String
    public let expires_at: String
    public let receipts: [EvieIntentReceipt]
}

/// Minimal type-erasing JSON value so intent args can round-trip.
public enum AnyCodableValue: Codable, Sendable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case object([String: AnyCodableValue])
    case array([AnyCodableValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([String: AnyCodableValue].self) { self = .object(v) }
        else if let v = try? c.decode([AnyCodableValue].self) { self = .array(v) }
        else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "AnyCodableValue") }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .null: try c.encodeNil()
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        }
    }
}

public struct EvieIntentList: Codable, Sendable {
    public let ok: Bool
    public let intents: [EvieIntent]
}

public struct EviePrimaryResponse: Codable, Sendable {
    public let ok: Bool
    public let primary: EviePresenceDevice?
}

public struct EviePresenceDevice: Codable, Sendable, Equatable, Identifiable {
    public var id: String { device_id }
    public let device_id: String
    public let display_name: String
    public let device_type: String?
    public let role: String?
    public let platform: String?
    public let presence_state: String
    public let last_seen_at: String?
    public let capabilities: [String]?
}

public struct EvieDeviceCandidate: Codable, Sendable {
    public let device_id: String
    public let display_name: String
    public let device_type: String?
    public let role: String?
    public let platform: String?
    public let presence_state: String
    public let last_seen_at: String?
    public let capabilities: [String]?
}

public struct EvieHudDevice: Codable, Sendable, Equatable, Identifiable {
    public var id: String { device_id }
    public let device_id: String
    public let display_name: String
    public let device_type: String?
    public let presence_state: String
    public let last_seen_at: String?
    public let recent_intent_kinds: [String]
    public let battery_percent: Double?
}

public struct EviePresenceHUD: Codable, Sendable {
    public let ok: Bool
    public let devices: [EvieHudDevice]
    public let primary_device_id: String?
    public let generated_at: String
}

public struct EvieClipboardItem: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let source_device_id: String
    public let kind: String
    public let text: String
    public let created_at: String
    public let expires_at: String
}

public struct EvieClipboardResponse: Codable, Sendable {
    public let ok: Bool
    public let items: [EvieClipboardItem]
}

public struct EvieClipboardPushResponse: Codable, Sendable {
    public let ok: Bool
    public let item: EvieClipboardItem
}

public struct EvieLookResponse: Codable, Sendable {
    public let ok: Bool
    public let intent_id: String
    public let target_device_id: String?
}

public struct EviePocketExecutiveResponse: Codable, Sendable {
    public let ok: Bool
    public let bundle: [String]
    public let intents: [EvieIntent]
}

public struct EvieRemoteRequestResponse: Codable, Sendable {
    public let ok: Bool
    public let request_id: String
    public let status: String
}

public struct EvieWakeMirrorResponse: Codable, Sendable {
    public let ok: Bool
    public let intent_id: String
    public let mirror_thread_id: String
}

public struct EvieIntentPublishResponse: Codable, Sendable {
    public let ok: Bool
    public let intent: EvieIntent
    public let target: EvieDeviceCandidate?
}

public struct EvieIntentAckResponse: Codable, Sendable {
    public let ok: Bool
    public let intent: EvieIntent
}

private struct PublishBody: Encodable {
    let kind: String
    let capability: String?
    let args: [String: AnyCodableValue]
    let ttl_seconds: Int
}

private struct AckBody: Encodable {
    let status: String
    let note: String
}

private struct ClipboardPushBody: Encodable {
    let kind: String
    let text: String
    let ttl_seconds: Int
}

extension EVAPIClient {
    public func publishIntent(
        kind: String,
        capability: String? = nil,
        args: [String: AnyCodableValue] = [:],
        ttlSeconds: Int = 900
    ) async throws -> EvieIntentPublishResponse {
        let (_, data) = try await send(
            "/v1/everywhere/intents",
            method: "POST",
            body: try encode(PublishBody(kind: kind, capability: capability, args: args, ttl_seconds: ttlSeconds))
        )
        return try JSONDecoder().decode(EvieIntentPublishResponse.self, from: data)
    }

    public func listIntents(status: String? = nil, kind: String? = nil) async throws -> EvieIntentList {
        var items: [URLQueryItem] = []
        if let status { items.append(URLQueryItem(name: "status", value: status)) }
        if let kind { items.append(URLQueryItem(name: "kind", value: kind)) }
        let (_, data) = try await send("/v1/everywhere/intents", queryItems: items)
        return try JSONDecoder().decode(EvieIntentList.self, from: data)
    }

    public func ackIntent(id: String, status: String, note: String = "") async throws -> EvieIntentAckResponse {
        let (_, data) = try await send(
            "/v1/everywhere/intents/\(id)/ack",
            method: "POST",
            body: try encode(AckBody(status: status, note: note))
        )
        return try JSONDecoder().decode(EvieIntentAckResponse.self, from: data)
    }

    public func fetchPrimaryDevice() async throws -> EviePrimaryResponse {
        let (_, data) = try await send("/v1/everywhere/primary")
        return try JSONDecoder().decode(EviePrimaryResponse.self, from: data)
    }

    public func fetchPresenceHUD() async throws -> EviePresenceHUD {
        let (_, data) = try await send("/v1/everywhere/presence/hud")
        return try JSONDecoder().decode(EviePresenceHUD.self, from: data)
    }

    public func pushClipboard(kind: String = "text", text: String, ttlSeconds: Int = 1800) async throws -> EvieClipboardPushResponse {
        let (_, data) = try await send(
            "/v1/everywhere/clipboard/push",
            method: "POST",
            body: try encode(ClipboardPushBody(kind: kind, text: text, ttl_seconds: ttlSeconds))
        )
        return try JSONDecoder().decode(EvieClipboardPushResponse.self, from: data)
    }

    public func listClipboard() async throws -> EvieClipboardResponse {
        let (_, data) = try await send("/v1/everywhere/clipboard")
        return try JSONDecoder().decode(EvieClipboardResponse.self, from: data)
    }

    public func requestLook(reason: String = "look", kind: String = "photo") async throws -> EvieLookResponse {
        let body = try JSONSerialization.data(withJSONObject: ["reason": reason, "kind": kind])
        let (_, data) = try await send("/v1/everywhere/look/request", method: "POST", body: body)
        return try JSONDecoder().decode(EvieLookResponse.self, from: data)
    }

    public func headingOut() async throws -> EviePocketExecutiveResponse {
        let (_, data) = try await send("/v1/everywhere/pocket-executive/heading-out", method: "POST", body: try encode(EmptyBody()))
        return try JSONDecoder().decode(EviePocketExecutiveResponse.self, from: data)
    }

    public func requestRemote(action: String, deviceId: String? = nil) async throws -> EvieRemoteRequestResponse {
        var payload: [String: String] = ["action": action]
        if let deviceId { payload["device_id"] = deviceId }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let (_, data) = try await send("/v1/everywhere/remote/request", method: "POST", body: body)
        return try JSONDecoder().decode(EvieRemoteRequestResponse.self, from: data)
    }

    public func mirrorWake(threadId: String, deviceLabel: String) async throws -> EvieWakeMirrorResponse {
        let body = try JSONSerialization.data(withJSONObject: ["thread_id": threadId, "device_label": deviceLabel])
        let (_, data) = try await send("/v1/everywhere/wake/mirror", method: "POST", body: body)
        return try JSONDecoder().decode(EvieWakeMirrorResponse.self, from: data)
    }
}

private struct EmptyBody: Encodable {}
