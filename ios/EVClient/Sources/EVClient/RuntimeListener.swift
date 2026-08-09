import Foundation

/// A device heartbeat as reported to the 24/7 runtime.
public struct RuntimeHeartbeat: Codable, Sendable, Equatable {
    public let id: String
    public let deviceId: String
    public let reportedAt: String
    public let status: String
    public let listenerState: String
    public let batteryPercent: Double?
    public let latencyMs: Int?

    public init(
        id: String,
        deviceId: String,
        reportedAt: String,
        status: String,
        listenerState: String,
        batteryPercent: Double?,
        latencyMs: Int?
    ) {
        self.id = id
        self.deviceId = deviceId
        self.reportedAt = reportedAt
        self.status = status
        self.listenerState = listenerState
        self.batteryPercent = batteryPercent
        self.latencyMs = latencyMs
    }
}

/// One device candidate in a wake arbitration round.
public struct WakeCandidate: Codable, Sendable, Equatable {
    public let deviceId: String
    public let name: String
    public let score: Double
    public let selected: Bool
    public let reason: String

    public init(deviceId: String, name: String, score: Double, selected: Bool, reason: String) {
        self.deviceId = deviceId
        self.name = name
        self.score = score
        self.selected = selected
        self.reason = reason
    }
}

/// The fleet-wide outcome of a wake arbitration.
public struct WakeArbitration: Codable, Sendable, Equatable {
    public let winner: WakeCandidate?
    public let candidates: [WakeCandidate]
    public let state: String
    public let sessionId: String?
    public let blocked: Bool
    public let blockReason: String?

    public init(
        winner: WakeCandidate?,
        candidates: [WakeCandidate],
        state: String,
        sessionId: String?,
        blocked: Bool,
        blockReason: String?
    ) {
        self.winner = winner
        self.candidates = candidates
        self.state = state
        self.sessionId = sessionId
        self.blocked = blocked
        self.blockReason = blockReason
    }
}

/// Compact runtime state inside the cross-device sync snapshot.
public struct RuntimeSyncState: Decodable, Sendable, Equatable {
    public let state: String
    public let sessionId: String?
    public let sessionState: String?
    public let deviceId: String?
    public let quietHoursActive: Bool
    public let attention: [String: Int]
    public let deadLetters: [String: Int]
    public let actionsPending: Int
}

/// One fleet device inside the sync snapshot.
public struct RuntimeSyncDevice: Decodable, Sendable, Equatable {
    public let deviceId: String
    public let name: String
    public let presence: String
    public let listenerState: String?
    public let batteryPercent: Double?
    public let lastSeenAt: String?
    public let lastHeartbeatAt: String?
}

/// Wake-to-reply latency markers for the latest session.
public struct RuntimeLatency: Decodable, Sendable, Equatable {
    public let sessionId: String?
    public let wakeToAwakeMs: Int?
    public let wakeToProcessingMs: Int?
    public let wakeToRespondingMs: Int?
    public let wakeToFollowUpMs: Int?
}

/// One entry in the append-only runtime event feed.
public struct RuntimeEventOut: Decodable, Sendable, Equatable {
    public let id: String
    public let occurredAt: String
    public let kind: String
    public let deviceId: String?
    public let sessionId: String?
    public let actionId: String?
}

/// The cross-device runtime sync snapshot.
public struct RuntimeSync: Decodable, Sendable, Equatable {
    public let schemaVersion: String
    public let generatedAt: String
    public let runtime: RuntimeSyncState
    public let devices: [RuntimeSyncDevice]
    public let events: [RuntimeEventOut]
    public let latency: RuntimeLatency
    public let digest: DigestState?
}

/// The latest delivered alert digest (nil before any digest exists).
public struct DigestState: Decodable, Sendable, Equatable {
    public let digestId: String?
    public let delivered: Int
    public let source: String?
    public let generatedAt: String?
}

/// Headless listener: heartbeats the runtime, participates in wake arbitration,
/// and pulls the convergent sync snapshot — the iOS "ear" for the 24/7 runtime.
public struct RuntimeListener: Sendable {
    public let client: EVAPIClient

    public init(client: EVAPIClient) {
        self.client = client
    }

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    public func heartbeat(
        deviceID: String,
        status: String = "ok",
        listenerState: String = "listening",
        batteryPercent: Double? = nil,
        latencyMs: Int? = nil
    ) async throws -> RuntimeHeartbeat {
        struct Body: Encodable {
            let deviceId: String
            let status: String
            let listenerState: String
            let batteryPercent: Double?
            let latencyMs: Int?
        }
        let body = try Self.encoder.encode(
            Body(
                deviceId: deviceID,
                status: status,
                listenerState: listenerState,
                batteryPercent: batteryPercent,
                latencyMs: latencyMs
            )
        )
        let (_, data) = try await client.send(
            "/v1/runtime/heartbeat",
            method: "POST",
            body: body,
            allowedStatuses: [201]
        )
        return try Self.decoder.decode(RuntimeHeartbeat.self, from: data)
    }

    public func wake(
        deviceID: String,
        signalScore: Double = 0.5,
        proximityScore: Double = 0.5,
        priority: Double = 0.5,
        payload: [String: String] = [:]
    ) async throws -> WakeArbitration {
        struct Intent: Encodable {
            let deviceId: String
            let signalScore: Double
            let proximityScore: Double
            let priority: Double
            let payload: [String: String]
        }
        let body = try Self.encoder.encode(
            [
                Intent(
                    deviceId: deviceID,
                    signalScore: signalScore,
                    proximityScore: proximityScore,
                    priority: priority,
                    payload: payload
                )
            ]
        )
        let (_, data) = try await client.send(
            "/v1/runtime/wake",
            method: "POST",
            body: body,
            allowedStatuses: [200]
        )
        return try Self.decoder.decode(WakeArbitration.self, from: data)
    }

    public func syncState(since: String? = nil, limit: Int = 200) async throws -> RuntimeSync {
        var items = [URLQueryItem(name: "limit", value: String(limit))]
        if let since {
            items.append(URLQueryItem(name: "since", value: since))
        }
        let (_, data) = try await client.send(
            "/v1/runtime/sync",
            queryItems: items,
            allowedStatuses: [200]
        )
        return try Self.decoder.decode(RuntimeSync.self, from: data)
    }
}
