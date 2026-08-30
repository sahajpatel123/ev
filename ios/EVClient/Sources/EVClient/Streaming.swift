/// SSE streaming surfaces for the EV v1 API: chat deltas (Agent 10/CORTEX)
/// and voice utterance partials/replies (Agent 4/VOICE).

import Foundation

// MARK: - Chat stream

/// Terminal metadata from the `done` SSE event of `POST /v1/chat`.
public struct ChatStreamDone: Codable, Sendable, Equatable {
    public let conversationId: String?
    public let contextTokens: Int
    public let contextDepth: String?
    public let requestId: String?
    public let model: String?

    public init(
        conversationId: String?,
        contextTokens: Int,
        contextDepth: String?,
        requestId: String?,
        model: String?
    ) {
        self.conversationId = conversationId
        self.contextTokens = contextTokens
        self.contextDepth = contextDepth
        self.requestId = requestId
        self.model = model
    }
}

/// One parsed event from the streaming chat surface.
public enum ChatStreamEvent: Sendable, Equatable {
    case memoryDelta(MemoryDelta)
    case provenance(ProvenanceItem)
    case filterReport(AnyCodable)
    case contextPlan(AnyCodable)
    case delta(String, final: Bool)
    case refined(String)
    case status(String)
    case done(ChatStreamDone)
    case error(String)
}

private struct ChatStreamBody: Encodable {
    let message: String
    let stream: Bool
    let conversationId: String?
    let deviceId: String?
    let model: String?
    let contextDepth: String?
}

private struct DeltaPayload: Decodable {
    let text: String
    let final: Bool?
}

private struct RefinedPayload: Decodable {
    let text: String
    let replaces: Bool?
}

private struct ErrorPayload: Decodable {
    let message: String?
    let code: String?
}

// MARK: - Voice stream

/// One incremental ASR hypothesis (`partial` event).
public struct VoicePartialOut: Codable, Sendable, Equatable {
    public let text: String
    public let provider: String
    public let sequence: Int
    public let stable: Bool
    public let confidence: Double
    public let degraded: Bool
    public let timestampMs: Int?
}

/// The final transcript (`final_transcript` event).
public struct VoiceTranscriptOut: Codable, Sendable, Equatable {
    public let text: String
    public let confidence: Double
    public let provider: String?
    public let degraded: Bool?
    public let audioRef: String?
}

/// One parsed event from `POST /v1/voice/utterance/stream`.
public enum VoiceStreamEvent: Sendable, Equatable {
    case partial(VoicePartialOut)
    case transcript(VoiceTranscriptOut)
    case ttsChunk(VoiceTtsChunk)
    case reply(VoiceUtteranceResponse)
    case error(String)
    case done
}

/// First-word (and later clause) TTS audio streamed before the full reply.
public struct VoiceTtsChunk: Codable, Sendable, Equatable {
    public let index: Int
    public let text: String
    public let audioB64: String?
    public let contentType: String?
    public let durationMs: Int?
    public let provider: String?
}

// MARK: - Client extensions

extension EVAPIClient {
    /// Stream a chat reply from `POST /v1/chat` (SSE).
    ///
    /// The backend emits `status` while the pipeline is still working,
    /// then `memory-delta`, `provenance`, `filter-report`, `context-plan`,
    /// progressive `delta` chunks, `refined` (replace semantics after
    /// output filtering), `done`, and `error` events.
    public func askStream(
        _ question: String,
        conversationId: String? = nil,
        deviceId: String? = nil,
        model: String? = nil,
        contextDepth: String? = nil
    ) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let encoder = JSONEncoder()
                    encoder.keyEncodingStrategy = .convertToSnakeCase
                    let body = try encoder.encode(
                        ChatStreamBody(
                            message: question,
                            stream: true,
                            conversationId: conversationId,
                            deviceId: deviceId,
                            model: model,
                            contextDepth: contextDepth
                        )
                    )
                    var request = URLRequest(url: Self.streamURL(baseURL: baseURL, path: "/v1/chat"))
                    request.httpMethod = "POST"
                    request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.httpBody = body

                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw EVAPIError.transport("non-HTTP response")
                    }
                    guard http.statusCode == 200 else {
                        throw EVAPIError.httpStatus(http.statusCode, "stream request failed")
                    }

                    var eventName = ""
                    var dataLines: [String] = []
                    for try await line in bytes.lines {
                        if line.hasPrefix("event:") {
                            if !eventName.isEmpty || !dataLines.isEmpty {
                                try Self.flushChat(
                                    name: eventName,
                                    data: dataLines.joined(separator: "\n"),
                                    continuation: continuation
                                )
                                eventName = ""
                                dataLines = []
                            }
                            eventName = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces)
                        } else if line.hasPrefix("data:") {
                            dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
                        } else if line.isEmpty {
                            try Self.flushChat(
                                name: eventName,
                                data: dataLines.joined(separator: "\n"),
                                continuation: continuation
                            )
                            eventName = ""
                            dataLines = []
                        }
                    }
                    try Self.flushChat(
                        name: eventName,
                        data: dataLines.joined(separator: "\n"),
                        continuation: continuation
                    )
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    /// Stream a voice utterance (`POST /v1/voice/utterance/stream`, SSE).
    public func streamUtterance(
        sessionId: String,
        text: String? = nil,
        audioB64: String? = nil,
        audioRef: String? = nil,
        reverifyToken: String? = nil,
        language: String = "en",
        conversationId: String? = nil,
        followUp: Bool = false,
        pushToTalk: Bool = false
    ) -> AsyncThrowingStream<VoiceStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let encoder = JSONEncoder()
                    encoder.keyEncodingStrategy = .convertToSnakeCase
                    struct Body: Encodable {
                        let sessionId: String
                        let text: String?
                        let audioB64: String?
                        let audioRef: String?
                        let reverifyToken: String?
                        let language: String
                        let conversationId: String?
                        let followUp: Bool
                        let pushToTalk: Bool
                    }
                    let body = try encoder.encode(
                        Body(
                            sessionId: sessionId,
                            text: text,
                            audioB64: audioB64,
                            audioRef: audioRef,
                            reverifyToken: reverifyToken,
                            language: language,
                            conversationId: conversationId,
                            followUp: followUp,
                            pushToTalk: pushToTalk
                        )
                    )
                    var request = URLRequest(
                        url: Self.streamURL(baseURL: baseURL, path: "/v1/voice/utterance/stream")
                    )
                    request.httpMethod = "POST"
                    request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.httpBody = body

                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw EVAPIError.transport("non-HTTP response")
                    }
                    guard http.statusCode == 200 else {
                        var collected = Data()
                        do {
                            for try await byte in bytes {
                                collected.append(byte)
                                if collected.count >= 800 { break }
                            }
                        } catch {
                            // Body may already be closed; the status is enough.
                        }
                        throw EVAPIError.httpStatus(
                            http.statusCode,
                            collected.isEmpty
                                ? "stream request failed"
                                : EVAPIClient.apiErrorDetail(collected)
                        )
                    }

                    var eventName = ""
                    var dataLines: [String] = []
                    for try await line in bytes.lines {
                        if line.hasPrefix("event:") {
                            if !eventName.isEmpty || !dataLines.isEmpty {
                                Self.flushVoice(
                                    name: eventName,
                                    data: dataLines.joined(separator: "\n"),
                                    continuation: continuation
                                )
                                eventName = ""
                                dataLines = []
                            }
                            eventName = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces)
                        } else if line.hasPrefix("data:") {
                            dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
                        } else if line.isEmpty {
                            Self.flushVoice(
                                name: eventName,
                                data: dataLines.joined(separator: "\n"),
                                continuation: continuation
                            )
                            eventName = ""
                            dataLines = []
                        }
                    }
                    Self.flushVoice(
                        name: eventName,
                        data: dataLines.joined(separator: "\n"),
                        continuation: continuation
                    )
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    // MARK: Private helpers

    private static func streamURL(baseURL: URL, path: String) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent(path.hasPrefix("/") ? String(path.dropFirst()) : path),
            resolvingAgainstBaseURL: false
        )
        return components?.url ?? baseURL
    }

    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    private static func flushChat(
        name: String,
        data: String,
        continuation: AsyncThrowingStream<ChatStreamEvent, Error>.Continuation
    ) throws {
        guard !name.isEmpty, !data.isEmpty else { return }
        let decoder = decoder()
        let payload = Data(data.utf8)
        switch name {
        case "memory-delta":
            continuation.yield(.memoryDelta(try decoder.decode(MemoryDelta.self, from: payload)))
        case "provenance":
            continuation.yield(.provenance(try decoder.decode(ProvenanceItem.self, from: payload)))
        case "filter-report":
            continuation.yield(.filterReport(try decoder.decode(AnyCodable.self, from: payload)))
        case "context-plan":
            continuation.yield(.contextPlan(try decoder.decode(AnyCodable.self, from: payload)))
        case "status":
            struct StatusPayload: Decodable { let stage: String? }
            let status = try decoder.decode(StatusPayload.self, from: payload)
            continuation.yield(.status(status.stage ?? "thinking"))
        case "delta":
            let delta = try decoder.decode(DeltaPayload.self, from: payload)
            continuation.yield(.delta(delta.text, final: delta.final ?? false))
        case "refined":
            let refined = try decoder.decode(RefinedPayload.self, from: payload)
            continuation.yield(.refined(refined.text))
        case "done":
            continuation.yield(.done(try decoder.decode(ChatStreamDone.self, from: payload)))
        case "error":
            let error = try decoder.decode(ErrorPayload.self, from: payload)
            continuation.yield(.error(error.message ?? error.code ?? "unknown stream error"))
        default:
            break
        }
    }

    private static func flushVoice(
        name: String,
        data: String,
        continuation: AsyncThrowingStream<VoiceStreamEvent, Error>.Continuation
    ) {
        guard !name.isEmpty, !data.isEmpty else { return }
        let decoder = decoder()
        let payload = Data(data.utf8)
        do {
            switch name {
            case "partial":
                continuation.yield(.partial(try decoder.decode(VoicePartialOut.self, from: payload)))
            case "final_transcript":
                continuation.yield(.transcript(try decoder.decode(VoiceTranscriptOut.self, from: payload)))
            case "tts_chunk":
                continuation.yield(.ttsChunk(try decoder.decode(VoiceTtsChunk.self, from: payload)))
            case "reply":
                continuation.yield(.reply(try decoder.decode(VoiceUtteranceResponse.self, from: payload)))
            case "error":
                let error = try decoder.decode(ErrorPayload.self, from: payload)
                continuation.yield(.error(error.message ?? error.code ?? "unknown stream error"))
            case "done":
                continuation.yield(.done)
            default:
                break
            }
        } catch {
            continuation.yield(.error("Bad voice reply: \(error.localizedDescription)"))
        }
    }
}
