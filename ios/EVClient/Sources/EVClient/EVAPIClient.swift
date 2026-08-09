/// Thin async HTTP client for the EV v1 API (iPhone, Watch, Mac).

import Foundation

public enum EVAPIError: Error, Sendable {
    case httpStatus(Int, String)
    case transport(String)
    case decoding(String)
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
}

public struct EVAPIClient: Sendable {
    public let baseURL: URL
    public let token: String
    public let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
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
        allowedStatuses: Set<Int> = [200]
    ) async throws -> (Int, Data) {
        var request = URLRequest(url: url(for: path, queryItems: queryItems))
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        for (key, value) in headers {
            request.setValue(value, forHTTPHeaderField: key)
        }
        request.httpBody = body
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
                String(data: data, encoding: .utf8) ?? ""
            )
        }
        return (http.statusCode, data)
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

    private func encode<T: Encodable>(_ value: T) throws -> Data {
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
        let body = try encode(ChatRequestBody(message: question, stream: stream))
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

    public func health() async throws -> HealthResponse {
        let (_, data) = try await send("/v1/health")
        return try decode(HealthResponse.self, from: data)
    }
}
