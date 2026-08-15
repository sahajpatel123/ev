/// Thin async HTTP client for the EV v1 API (iPhone, Watch, Mac).

import Foundation

public enum EVAPIError: Error, Sendable, LocalizedError {
    case httpStatus(Int, String)
    case transport(String)
    case decoding(String)

    public var errorDescription: String? {
        switch self {
        case .httpStatus(let code, let body):
            let detail = body.trimmingCharacters(in: .whitespacesAndNewlines)
            if detail.isEmpty {
                return "API error \(code)"
            }
            return "API error \(code): \(detail.prefix(240))"
        case .transport(let message):
            return "Network error: \(message)"
        case .decoding(let message):
            return "Bad response: \(message)"
        }
    }
}

public struct CaptureResult: Sendable, Equatable {
    public let event: EventOut?
    public let memoryDeltas: [MemoryDelta]
    public let duplicate: Bool
    public let idempotencyKey: String

    public init(
        event: EventOut?,
        memoryDeltas: [MemoryDelta],
        duplicate: Bool,
        idempotencyKey: String
    ) {
        self.event = event
        self.memoryDeltas = memoryDeltas
        self.duplicate = duplicate
        self.idempotencyKey = idempotencyKey
    }
}

private struct ChatRequestBody: Encodable {
    let message: String
    let stream: Bool
    let conversationId: String?
    let deviceId: String?
    let model: String?
    let contextDepth: String?
}

private struct BriefingRequestBody: Encodable {
    let topic: String
    let stakes: String?
    let context: String?
}

private struct VoiceVerifyRequestBody: Encodable {
    let sessionId: String
    let nonce: String?
    let phrase: String?
    let samples: [String]
    let livenessProof: String?
    let liveScore: Double?
}

private struct VoiceWakeRequestBody: Encodable {
    let deviceId: String
    let wakeWord: String
    let audioRef: String?
    let textHint: String?
    let audioB64: String?
    let pushToTalk: Bool
}

private struct VoiceLiveOpenRequestBody: Encodable {
    let deviceId: String
}

private struct VoiceUtteranceRequestBody: Encodable {
    let sessionId: String
    let text: String?
    let followUp: Bool
    let audioB64: String?
    let audioRef: String?
    let reverifyToken: String?
    let language: String
    let conversationId: String?
    let pushToTalk: Bool
}

public struct EVAPIClient: Sendable {
    public let baseURL: URL
    public let token: String
    public let session: URLSession

    /// Voice turns (ASR + chat + TTS) can exceed URLSession.shared's 60s default.
    public static let voiceSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 90
        config.timeoutIntervalForResource = 180
        config.waitsForConnectivity = false
        config.httpMaximumConnectionsPerHost = 8
        return URLSession(configuration: config)
    }()

    public init(baseURL: URL, token: String, session: URLSession = EVAPIClient.voiceSession) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    // MARK: - Requests

    private func url(for path: String, queryItems: [URLQueryItem] = []) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent(path.hasPrefix("/") ? String(path.dropFirst()) : path),
            resolvingAgainstBaseURL: false
        )
        if !queryItems.isEmpty {
            components?.queryItems = queryItems
        }
        return components?.url ?? baseURL
    }

    func send(
        _ path: String,
        method: String = "GET",
        body: Data? = nil,
        headers: [String: String] = [:],
        queryItems: [URLQueryItem] = [],
        allowedStatuses: Set<Int> = [200],
        timeout: TimeInterval? = nil
    ) async throws -> (Int, Data) {
        var request = URLRequest(url: url(for: path, queryItems: queryItems))
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        for (key, value) in headers {
            request.setValue(value, forHTTPHeaderField: key)
        }
        request.httpBody = body
        request.timeoutInterval = timeout ?? 90
        return try await perform(request, allowedStatuses: allowedStatuses)
    }

    private func perform(
        _ request: URLRequest,
        allowedStatuses: Set<Int>
    ) async throws -> (Int, Data) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw EVAPIError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw EVAPIError.transport("non-HTTP response")
        }
        guard allowedStatuses.contains(http.statusCode) else {
            throw EVAPIError.httpStatus(
                http.statusCode,
                Self.apiErrorDetail(data)
            )
        }
        return (http.statusCode, data)
    }

    static func apiErrorDetail(_ data: Data) -> String {
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let detail = object["detail"] as? String {
                return detail
            }
            if let detail = object["detail"] {
                return String(describing: detail)
            }
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw EVAPIError.decoding(error.localizedDescription)
        }
    }

    func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return try encoder.encode(value)
    }

    // MARK: - Capture

    func postEvent(
        payload: CapturePayload,
        idempotencyKey: String
    ) async throws -> (Int, Data) {
        try await send(
            "/v1/events",
            method: "POST",
            body: encode(payload),
            headers: ["Idempotency-Key": idempotencyKey],
            allowedStatuses: [201, 400, 409, 422]
        )
    }

    public func capture(
        payload: CapturePayload,
        idempotencyKey: String = UUID().uuidString
    ) async throws -> CaptureResult {
        let (status, data) = try await postEvent(payload: payload, idempotencyKey: idempotencyKey)
        switch status {
        case 201, 409:
            let response = try decode(CaptureResponse.self, from: data)
            return CaptureResult(
                event: response.event,
                memoryDeltas: response.memoryDelta,
                duplicate: status == 409,
                idempotencyKey: idempotencyKey
            )
        default:
            throw EVAPIError.httpStatus(status, String(data: data, encoding: .utf8) ?? "")
        }
    }

    /// Upload a file/photo as an attachment event (camera, share sheet, files).
    public func attach(
        filename: String,
        contentType: String,
        data: Data,
        source: String = "ios",
        eventType: String = "file",
        privacyLevel: String = "normal",
        deviceID: String? = nil
    ) async throws -> AttachmentCreateResponse {
        let boundary = "Boundary-\(UUID().uuidString)"
        let safeFilename = filename
            .replacingOccurrences(of: "\"", with: "")
            .replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
        var body = Data()

        func appendField(_ name: String, _ value: String) {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
            body.append(Data("\(value)\r\n".utf8))
        }

        appendField("source", source)
        appendField("event_type", eventType)
        appendField("privacy_level", privacyLevel)
        if let deviceID {
            appendField("device_id", deviceID)
        }
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(safeFilename)\"\r\n".utf8
        ))
        body.append(Data("Content-Type: \(contentType)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))

        var request = URLRequest(url: url(for: "/v1/attachments"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        let responseData: Data
        let response: URLResponse
        do {
            (responseData, response) = try await session.upload(for: request, from: body)
        } catch {
            throw EVAPIError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw EVAPIError.transport("non-HTTP response")
        }
        guard http.statusCode == 201 else {
            throw EVAPIError.httpStatus(
                http.statusCode,
                String(data: responseData, encoding: .utf8) ?? ""
            )
        }
        return try decode(AttachmentCreateResponse.self, from: responseData)
    }

    // MARK: - Ask / browse

    public func ask(
        _ question: String,
        stream: Bool = false
    ) async throws -> ChatResponse {
        let body = try encode(
            ChatRequestBody(
                message: question,
                stream: stream,
                conversationId: nil,
                deviceId: nil,
                model: nil,
                contextDepth: nil
            )
        )
        let (_, data) = try await send(
            "/v1/chat",
            method: "POST",
            body: body,
            allowedStatuses: [200]
        )
        return try decode(ChatResponse.self, from: data)
    }

    public func timeline(limit: Int = 50) async throws -> TimelineResponse {
        let (_, data) = try await send(
            "/v1/timeline",
            queryItems: [URLQueryItem(name: "limit", value: String(limit))]
        )
        return try decode(TimelineResponse.self, from: data)
    }

    public func memories(
        memoryType: String? = nil,
        query: String? = nil,
        limit: Int = 50
    ) async throws -> MemoryListResponse {
        var items = [URLQueryItem(name: "limit", value: String(limit))]
        if let memoryType {
            items.append(URLQueryItem(name: "memory_type", value: memoryType))
        }
        if let query {
            items.append(URLQueryItem(name: "q", value: query))
        }
        let (_, data) = try await send("/v1/memories", queryItems: items)
        return try decode(MemoryListResponse.self, from: data)
    }

    public func audit(memoryId: String) async throws -> AuditResponse {
        let (_, data) = try await send("/v1/audit/\(memoryId)")
        return try decode(AuditResponse.self, from: data)
    }

    public func hudCard() async throws -> HUDCard {
        let (_, data) = try await send("/v1/hud/card")
        let card = try decode(HUDCard.self, from: data)
        try card.validate()
        return card
    }

    public func lookoutUtterance(
        text: String,
        conversationId: String? = nil,
        preferHaptic: Bool = true
    ) async throws -> LookoutUtteranceResult {
        struct Body: Encodable {
            let text: String
            let conversationId: String?
            let preferHaptic: Bool
        }
        let (_, data) = try await send(
            "/v1/lookout/utterance",
            method: "POST",
            body: encode(Body(text: text, conversationId: conversationId, preferHaptic: preferHaptic))
        )
        return try decode(LookoutUtteranceResult.self, from: data)
    }

    public func quickCard(
        topic: String,
        stakes: String? = nil,
        context: String? = nil,
        ttlSeconds: Int = 3600
    ) async throws -> HUDQuickCard {
        var items = [
            URLQueryItem(name: "topic", value: topic),
            URLQueryItem(name: "ttl_seconds", value: String(ttlSeconds)),
        ]
        if let stakes {
            items.append(URLQueryItem(name: "stakes", value: stakes))
        }
        if let context {
            items.append(URLQueryItem(name: "context", value: context))
        }
        let (_, data) = try await send("/v1/tactical/quick", queryItems: items)
        let card = try decode(HUDQuickCard.self, from: data)
        try card.validate()
        return card
    }

    public func postHealthSnapshot(
        source: String,
        deviceId: String? = nil,
        metrics: [String: Double]
    ) async throws {
        struct Body: Encodable {
            let source: String
            let deviceId: String?
            let metrics: [String: Double]
        }
        _ = try await send(
            "/v1/health/snapshot",
            method: "POST",
            body: encode(Body(source: source, deviceId: deviceId, metrics: metrics)),
            allowedStatuses: [201]
        )
    }

    public func health() async throws -> HealthResponse {
        let (_, data) = try await send("/v1/health", timeout: 8)
        return try decode(HealthResponse.self, from: data)
    }

    public func conversation(limit: Int = 50) async throws -> ConversationDetail {
        let (_, data) = try await send(
            "/v1/conversation",
            queryItems: [URLQueryItem(name: "limit", value: String(limit))],
            timeout: 15
        )
        return try decode(ConversationDetail.self, from: data)
    }

    /// Fetch persisted TTS bytes. ``ref`` may be ``ev://voice/tts/...`` or a store key.
    public func voiceAudio(ref: String) async throws -> Data {
        var key = ref
        if key.hasPrefix("ev://") {
            key = String(key.dropFirst(5))
        }
        while key.hasPrefix("/") {
            key = String(key.dropFirst())
        }
        let (_, data) = try await send("/v1/voice/audio/\(key)", timeout: 20)
        return data
    }

    public func wakeVoice(
        deviceId: String,
        wakeWord: String = "evie",
        audioRef: String? = nil,
        textHint: String? = nil,
        audioB64: String? = nil,
        pushToTalk: Bool = false
    ) async throws -> VoiceWakeResponse {
        let body = try encode(
            VoiceWakeRequestBody(
                deviceId: deviceId,
                wakeWord: wakeWord,
                audioRef: audioRef,
                textHint: textHint,
                audioB64: audioB64,
                pushToTalk: pushToTalk
            )
        )
        let (_, data) = try await send(
            "/v1/voice/wake",
            method: "POST",
            body: body,
            allowedStatuses: [200, 201],
            timeout: 20
        )
        return try decode(VoiceWakeResponse.self, from: data)
    }

    /// Open a full-duplex live conversation without a wake word.
    public func openLiveVoice(deviceId: String) async throws -> VoiceLiveOpenResponse {
        let body = try encode(VoiceLiveOpenRequestBody(deviceId: deviceId))
        let (_, data) = try await send(
            "/v1/voice/live/open",
            method: "POST",
            body: body,
            allowedStatuses: [200, 201],
            timeout: 20
        )
        return try decode(VoiceLiveOpenResponse.self, from: data)
    }

    public func verifyVoice(
        sessionId: String,
        nonce: String? = nil,
        phrase: String? = nil,
        samples: [String] = [],
        livenessProof: String? = nil,
        liveScore: Double? = nil
    ) async throws -> VoiceSessionVerifyResponse {
        let body = try encode(
            VoiceVerifyRequestBody(
                sessionId: sessionId,
                nonce: nonce,
                phrase: phrase,
                samples: samples,
                livenessProof: livenessProof,
                liveScore: liveScore
            )
        )
        let (_, data) = try await send("/v1/voice/verify", method: "POST", body: body)
        return try decode(VoiceSessionVerifyResponse.self, from: data)
    }

    public func utterance(
        sessionId: String,
        text: String? = nil,
        followUp: Bool = false,
        audioB64: String? = nil,
        audioRef: String? = nil,
        reverifyToken: String? = nil,
        language: String = "en",
        conversationId: String? = nil,
        pushToTalk: Bool = false
    ) async throws -> VoiceUtteranceResponse {
        let body = try encode(
            VoiceUtteranceRequestBody(
                sessionId: sessionId,
                text: text,
                followUp: followUp,
                audioB64: audioB64,
                audioRef: audioRef,
                reverifyToken: reverifyToken,
                language: language,
                conversationId: conversationId,
                pushToTalk: pushToTalk
            )
        )
        let (_, data) = try await send(
            "/v1/voice/utterance",
            method: "POST",
            body: body,
            timeout: 90
        )
        return try decode(VoiceUtteranceResponse.self, from: data)
    }

    // MARK: - Live data / sensors

    /// Upload a live-event batch (`POST /v1/live/events`).
    public func postLiveBatch(_ batch: LiveBatchRequest) async throws -> [LiveEventOut] {
        let body = try encode(batch)
        let (_, data) = try await send(
            "/v1/live/events",
            method: "POST",
            body: body,
            allowedStatuses: [201]
        )
        return try decode([LiveEventOut].self, from: data)
    }

    /// Append events to an existing live channel
    /// (`POST /v1/live/channels/{id}/events`).
    public func postLiveEvents(
        _ events: [LiveEventCreate],
        toChannel channelID: String
    ) async throws -> [LiveEventOut] {
        let body = try encode(events)
        let (_, data) = try await send(
            "/v1/live/channels/\(channelID)/events",
            method: "POST",
            body: body,
            allowedStatuses: [201]
        )
        return try decode([LiveEventOut].self, from: data)
    }

    public func tacticalBrief(
        topic: String,
        stakes: String? = nil,
        context: String? = nil
    ) async throws -> HUDBriefing {
        let body = try encode(
            BriefingRequestBody(topic: topic, stakes: stakes, context: context)
        )
        let (_, data) = try await send("/v1/tactical/brief", method: "POST", body: body)
        let briefing = try decode(HUDBriefing.self, from: data)
        try briefing.validate()
        return briefing
    }

    public func focusHud() async throws -> HUDFocus {
        let (_, data) = try await send("/v1/hud/focus")
        let focus = try decode(HUDFocus.self, from: data)
        try focus.validate()
        return focus
    }

    public func routeHud() async throws -> HUDRoute {
        let (_, data) = try await send("/v1/hud/route")
        let route = try decode(HUDRoute.self, from: data)
        try route.validate()
        return route
    }

    public func createDevice(
        name: String,
        capabilities: [String],
        deviceType: String = "unknown"
    ) async throws -> DeviceCreateResponse {
        struct Body: Encodable {
            let name: String
            let capabilities: [String]
            let deviceType: String
        }
        let (_, data) = try await send(
            "/v1/devices",
            method: "POST",
            body: encode(Body(name: name, capabilities: capabilities, deviceType: deviceType)),
            allowedStatuses: [200, 201]
        )
        return try decode(DeviceCreateResponse.self, from: data)
    }

    public func bootstrapDevice(id: String) async throws -> DeviceBootstrap {
        let (_, data) = try await send("/v1/devices/\(id)/bootstrap")
        return try decode(DeviceBootstrap.self, from: data)
    }

    public func panicDevice(id: String) async throws -> DevicePanic {
        let (_, data) = try await send(
            "/v1/devices/\(id)/panic",
            method: "POST",
            body: Data("{}".utf8),
            allowedStatuses: [200, 201]
        )
        return try decode(DevicePanic.self, from: data)
    }

    public func lockAll() async throws -> DevicePanic {
        let (_, data) = try await send(
            "/v1/runtime/lock-all",
            method: "POST",
            body: Data("{}".utf8),
            allowedStatuses: [200, 201]
        )
        return try decode(DevicePanic.self, from: data)
    }
}
