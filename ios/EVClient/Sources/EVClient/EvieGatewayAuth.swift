/// Unified device-gateway auth for native shells (iPhone/Mac hub).
///
/// Legacy path (kept until backend parity cutover): `EVClientAppConfig.apiKey`
/// read from `KeychainTokenStore(account: "api")` and sent as
/// `Bearer <token>` on every `EVAPIClient` call.
///
/// Gateway path (this file): one-time `pairing_token` ->
/// long-lived `device_token` + short-lived `evie1` access token.
/// Access tokens expire (~15 min, see `device_access_ttl_seconds`);
/// refresh via `POST /v1/device-gateway/session` using the device token.
/// Live voice opens via `POST /v1/device-gateway/live/open`, which returns a
/// one-time `ws_ticket` for `WS /v1/voice/live?session_id=&ticket=`.
/// Long-lived tokens NEVER go in URLs.

import Foundation

// MARK: - Token model

/// Parsed `evie1.<deviceId>|<exp>|<scope>|<revision>.<digest>` access token.
/// Client-side parse only (no signature verification — server owns that);
/// used for proactive refresh before expiry.
public struct EvieAccessToken: Sendable, Equatable {
    public let raw: String
    public let deviceID: String
    public let expiresAt: Date
    public let scope: String
    public let revision: Int

    public init?(raw: String) {
        guard raw.hasPrefix("evie1.") else { return nil }
        let rest = String(raw.dropFirst("evie1.".count))
        guard let dot = rest.lastIndex(of: ".") else { return nil }
        let payload = String(rest[..<dot])
        let parts = payload.split(separator: "|", omittingEmptySubsequences: false)
        guard parts.count == 3 || parts.count == 4,
              let exp = TimeInterval(parts[1])
        else { return nil }
        self.raw = raw
        self.deviceID = String(parts[0])
        self.expiresAt = Date(timeIntervalSince1970: exp)
        self.scope = String(parts[2])
        self.revision = parts.count == 4 ? Int(parts[3]) ?? 0 : 0
    }

    /// True when the token expires within `margin` (default 60s).
    public func isExpiring(within margin: TimeInterval = 60) -> Bool {
        expiresAt.timeIntervalSinceNow < margin
    }
}

/// Gateway credential bundle. `deviceToken` is long-lived (keychain);
/// `accessToken` is short-lived (memory + keychain backup).
public struct EvieGatewayCredentials: Sendable, Equatable {
    public let deviceID: String
    public var deviceToken: String
    public var accessToken: String

    public init(deviceID: String, deviceToken: String, accessToken: String) {
        self.deviceID = deviceID
        self.deviceToken = deviceToken
        self.accessToken = accessToken
    }
}

// MARK: - Keychain accounts

public enum EvieGatewayKeychainAccounts {
    public static let deviceToken = "gateway-device-token"
    public static let accessToken = "gateway-access-token"
    public static let deviceID = "gateway-device-id"
    /// Legacy account — do NOT write new code against it; see migration list below.
    public static let legacyAPI = "api"
}

// MARK: - Wire models (snake_case via convertFromSnakeCase)

public struct EvieGatewayPairResponse: Sendable, Decodable {
    public struct Device: Sendable, Decodable {
        public let id: String
        public let name: String?
    }
    public let device: Device
    public let deviceToken: String
    public let accessToken: String
}

public struct EvieGatewaySessionResponse: Sendable, Decodable {
    public let accessToken: String
}

public struct EvieGatewayLiveTicket: Sendable, Decodable {
    public let sessionId: String
    public let wsPath: String?
    public let wsTicket: String?
    public let authRevision: Int?

    /// `WS <base>/<wsPath>?session_id=<id>&ticket=<ticket>`.
    public func socketURL(baseURL: URL) -> URL? {
        guard let ticket = wsTicket, !ticket.isEmpty else { return nil }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { return nil }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let livePath = (wsPath ?? "/v1/voice/live").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let prefix = basePath.isEmpty ? "" : basePath + "/"
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.path = "/" + prefix + livePath
        components.queryItems = [
            URLQueryItem(name: "session_id", value: sessionId),
            URLQueryItem(name: "ticket", value: ticket),
        ]
        return components.url
    }
}

// MARK: - Client

/// Gateway auth client: pair -> refresh -> live ticket.
/// All requests are `POST <base>/v1/device-gateway/...`.
public struct EvieGatewayAuth: Sendable {
    public static let gatewayPrefix = "/v1/device-gateway"
    public static let protocolVersion = "1"

    public let baseURL: URL
    public let session: URLSession

    public init(baseURL: URL, session: URLSession = EVAPIClient.voiceSession) {
        self.baseURL = baseURL
        self.session = session
    }

    // MARK: Pair

    public struct PairOptions: Sendable {
        public let displayName: String?
        public let capabilities: [String]
        public let clientVersion: String?
        public let platform: String
        public let instanceID: String

        public init(
            displayName: String? = nil,
            capabilities: [String] = ["foreground_voice", "camera", "text"],
            clientVersion: String? = nil,
            platform: String = "ios",
            instanceID: String = ""
        ) {
            self.displayName = displayName
            self.capabilities = capabilities
            self.clientVersion = clientVersion
            self.platform = platform
            self.instanceID = instanceID
        }
    }

    /// Exchange a one-time pairing token for gateway credentials.
    public func pair(pairingToken: String, options: PairOptions = PairOptions()) async throws -> EvieGatewayCredentials {
        var payload: [String: Any] = [
            "pairing_token": pairingToken,
            "capabilities": options.capabilities,
            "protocol_version": Self.protocolVersion,
            "platform": options.platform,
            "instance_id": options.instanceID,
            "native_shell": true,
        ]
        if let name = options.displayName { payload["display_name"] = name }
        if let version = options.clientVersion { payload["client_version"] = version }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let (status, data) = try await request("pair", method: "POST", bearer: nil, body: body)
        guard status == 200 || status == 201 else {
            throw EVAPIError.httpStatus(status, EVAPIClient.apiErrorDetail(data))
        }
        let decoded = try decode(EvieGatewayPairResponse.self, from: data)
        return EvieGatewayCredentials(
            deviceID: decoded.device.id,
            deviceToken: decoded.deviceToken,
            accessToken: decoded.accessToken
        )
    }

    // MARK: Refresh

    /// Mint a fresh short-lived access token. Send the long-lived
    /// device token as bearer (server also accepts an unexpired access token).
    public func refresh(deviceToken: String) async throws -> String {
        let (status, data) = try await request("session", method: "POST", bearer: deviceToken, body: Data())
        guard status == 200 else {
            throw EVAPIError.httpStatus(status, EVAPIClient.apiErrorDetail(data))
        }
        return try decode(EvieGatewaySessionResponse.self, from: data).accessToken
    }

    /// Refresh only when the access token is missing or expiring.
    /// Returns a valid access token (possibly the input).
    public func ensureAccessToken(_ credentials: inout EvieGatewayCredentials) async throws -> String {
        if let parsed = EvieAccessToken(raw: credentials.accessToken), !parsed.isExpiring() {
            return credentials.accessToken
        }
        let fresh = try await refresh(deviceToken: credentials.deviceToken)
        credentials.accessToken = fresh
        return fresh
    }

    // MARK: Live ticket

    /// Open a live session and return the one-time WS ticket.
    /// Caller connects `ticket.socketURL(baseURL:)` within ~45s; single use.
    /// When `relayed` is true the request carries `x-evie-relayed` so the
    /// backend rewrites `ws_path` to the relay WS
    /// (`/v1/device-gateway/relay/live/ws`); the ticket itself is unchanged
    /// (same session binding, same single-use TTL).
    public func liveTicket(accessToken: String, instanceID: String = "", relayed: Bool = false) async throws -> EvieGatewayLiveTicket {
        let payload: [String: Any] = ["instance_id": instanceID]
        let body = try JSONSerialization.data(withJSONObject: payload)
        let headers = relayed ? ["x-evie-relayed": "v1device-relay"] : [:]
        let (status, data) = try await request("live/open", method: "POST", bearer: accessToken, body: body, headers: headers)
        guard status == 200 else {
            throw EVAPIError.httpStatus(status, EVAPIClient.apiErrorDetail(data))
        }
        return try decode(EvieGatewayLiveTicket.self, from: data)
    }

    // MARK: Persistence helpers

    public func saveToKeychain(_ credentials: EvieGatewayCredentials, store: KeychainTokenStore = KeychainTokenStore()) throws {
        try store.save(token: credentials.deviceToken, account: EvieGatewayKeychainAccounts.deviceToken)
        try store.save(token: credentials.accessToken, account: EvieGatewayKeychainAccounts.accessToken)
        try store.save(token: credentials.deviceID, account: EvieGatewayKeychainAccounts.deviceID)
    }

    public func loadFromKeychain(store: KeychainTokenStore = KeychainTokenStore()) throws -> EvieGatewayCredentials? {
        guard let deviceToken = try store.load(account: EvieGatewayKeychainAccounts.deviceToken),
              let accessToken = try store.load(account: EvieGatewayKeychainAccounts.accessToken),
              let deviceID = try store.load(account: EvieGatewayKeychainAccounts.deviceID)
        else { return nil }
        return EvieGatewayCredentials(deviceID: deviceID, deviceToken: deviceToken, accessToken: accessToken)
    }

    // MARK: - Private HTTP

    private func request(
        _ path: String,
        method: String,
        bearer: String?,
        body: Data?,
        headers: [String: String] = [:]
    ) async throws -> (Int, Data) {
        var request = URLRequest(url: baseURL.appendingPathComponent(
            (Self.gatewayPrefix + "/" + path).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        ))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let bearer { request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
        for (field, value) in headers { request.setValue(value, forHTTPHeaderField: field) }
        request.httpBody = body
        request.timeoutInterval = 20
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw EVAPIError.transport("non-HTTP response")
            }
            return (http.statusCode, data)
        } catch {
            if EVAPIClient.isCancellation(error) { throw CancellationError() }
            throw EVAPIError.transport(error.localizedDescription)
        }
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
}

// MARK: - Call-site migration list (legacy -> gateway)
//
// Cutover needs backend parity (native `platform` + `native_shell` handling
// in `POST /v1/device-gateway/pair`), so the legacy path stays until then.
// When ready, migrate in this order:
//
// 1. `ClientConfig.swift:18` — `EVClientAppConfig.apiKey` loads
//    `KeychainTokenStore().load()` (account `"api"`). Replace with
//    `EvieGatewayAuth.loadFromKeychain()` + `ensureAccessToken` refresh;
//    keep `apiKey` as a deprecated fallback until all callers migrate.
// 2. `ios/EVApp/AppConfig.swift:11` — wraps `EVClientAppConfig()`; picks up
//    the new auth automatically once (1) lands. No direct edit needed.
// 3. `ios/EVApp/AppState.swift:14` — `EVAPIClient(baseURL:token:)`
//    construction; switch `token:` to the gateway access token.
// 4. `ios/EVApp/AppIntents.swift:16,49` — two `EVAPIClient(baseURL:token:)`
//    call sites (capture + converge intents); same token swap as (3).
// 5. `ios/EVShareExtension/ShareViewController.swift:76` — share-extension
//    capture client; same token swap (shares keychain access group).
// 6. `ios/EVWatchApp/ContentView.swift:53,62,91,108` — four watch clients
//    (HUD card, confirm, utterance, capture); same token swap.
// 7. `EVAPIClient.swift:167,354,428` — `Bearer <token>` send sites stay as-is
//    (they already send the access token); only the token *source* changes.
// 8. `Streaming.swift:143,249` — SSE `Bearer` headers; same as (7).
// 9. `LiveVoice.swift:191-211` — `LiveVoiceConnection.connect(sessionId:)`
//    puts the long-lived token in `?token=` + `Authorization` header.
//    Replace with `EvieGatewayAuth.liveTicket()` + `ticket.socketURL()`
//    (`?session_id=&ticket=`, no bearer header — ticket IS the credential).
// 10. `EVAPIClient.swift:806-816` — `openLiveVoice(deviceId:)` hits legacy
//    `POST /v1/voice/live/open`; replace with gateway `live/open` (this
//    file) which is what mints the `ws_ticket`.
// 11. `LifeAccess.swift:206-209` — `serverURL` + `storedKeyPresent` legacy
//    keychain check; point at gateway accounts.
// 12. `KeychainTokenStore.swift` — no change needed (generic store); retire
//    the default `account: "api"` only after (1)-(11) are done.
// MARK: - Gateway credential provider + EVAPIClient adapter (cutover)
//
// Append-only cutover: existing `EvieGatewayAuth` + `EVAPIClient.send`
// signatures are unchanged. New call sites resolve the bearer via
// `GatewayCredentialProvider` and send through `sendWithGatewayAuth`,
// which falls back to the legacy `EVAPIClient.token` (apiKey) when the
// gateway is disabled (`provider == nil`).

/// Actor-guarded gateway credentials: refreshes the short-lived access
/// token on demand via `EvieGatewayAuth.ensureAccessToken`.
public actor GatewayCredentialProvider: Sendable {
    private var credentials: EvieGatewayCredentials
    private let auth: EvieGatewayAuth

    public init(credentials: EvieGatewayCredentials, baseURL: URL, session: URLSession = EVAPIClient.voiceSession) {
        self.credentials = credentials
        self.auth = EvieGatewayAuth(baseURL: baseURL, session: session)
    }

    /// Valid access token, refreshing via the device token when expiring.
    public func ensureAccessToken() async throws -> String {
        var next = credentials
        let token = try await auth.ensureAccessToken(&next)
        credentials = next
        return token
    }

    /// Ready-to-send `Authorization` header value (`Bearer <access>`).
    public func authorizationHeaderValue() async throws -> String {
        "Bearer \(try await ensureAccessToken())"
    }
}

public extension EVAPIClient {
    /// Gateway-auth send. When `provider` is nil (gateway disabled), falls
    /// back to the legacy `token` (apiKey) path via `send`. Refresh failures
    /// propagate — only a disabled gateway falls back, never a bad refresh.
    func sendWithGatewayAuth(
        _ path: String,
        method: String = "GET",
        body: Data? = nil,
        headers: [String: String] = [:],
        queryItems: [URLQueryItem] = [],
        allowedStatuses: Set<Int> = [200],
        timeout: TimeInterval? = nil,
        provider: GatewayCredentialProvider? = nil
    ) async throws -> (Int, Data) {
        guard let provider else {
            return try await send(
                path, method: method, body: body, headers: headers,
                queryItems: queryItems, allowedStatuses: allowedStatuses, timeout: timeout
            )
        }
        let token = try await provider.ensureAccessToken()
        return try await EVAPIClient(baseURL: baseURL, token: token, session: session).send(
            path, method: method, body: body, headers: headers,
            queryItems: queryItems, allowedStatuses: allowedStatuses, timeout: timeout
        )
    }
}

// MARK: - Relay live voice channel
//
// When the relay route is enabled (`EvieRelayConfig.enabled` + a relay
// `EvieRouteDecision`), live voice opens via `POST <relayBase>/live/open`
// with `x-evie-relayed` (server rewrites `ws_path` to the relay WS) and the
// one-time ticket is consumed against `ticket.socketURL(baseURL: relayBase)`.
// The ticket IS the credential: no `Authorization` header, no long-lived
// token in the URL. Default direct path (`LiveVoiceConnection`) is untouched.

/// Minimal channel surface `LiveVoiceCoordinator` drives. `LiveVoiceConnection`
/// already satisfies it; `RelayLiveVoiceConnection` implements the same
/// contract over a ticket-authenticated relay WebSocket.
public protocol LiveVoiceChannel: AnyObject, Sendable {
    func connect(sessionId: String) async throws -> AsyncThrowingStream<LiveVoiceEvent, Error>
    func enqueuePCM(_ data: Data)
    func sendControl(_ action: String)
    func sendCamera(_ state: CameraState, deviceId: String?)
    func sendLookFrame(
        requestId: String?,
        jpeg: Data?,
        width: Int?,
        height: Int?,
        error: String?,
        permission: String?,
        deviceId: String?,
        attachmentId: String?,
        sequence: Int?,
        last: Bool?,
        cameraName: String?,
        luminance: Double?,
        labels: [String]?,
        ocrText: String?,
        faceCount: Int?,
        personCount: Int?,
        lighting: String?,
        colors: [String]?,
        savedPath: String?,
        mediaKind: String?,
        clipDurationMs: Int?,
        hasClip: Bool?,
        clipSupported: Bool?,
        capturedAtMs: Int?,
        streaming: Bool?,
        motionScore: Double?,
        placeHint: String?
    )
    func sendPlayback(active: Bool)
    func close()
}

extension LiveVoiceConnection: LiveVoiceChannel {}

public extension LiveVoiceChannel {
    /// Defaulted overload matching `LiveVoiceConnection.sendLookFrame` so
    /// existing call sites drive either channel identically.
    func sendLookFrame(
        requestId: String?,
        jpeg: Data?,
        width: Int?,
        height: Int?,
        error: String?,
        permission: String?,
        deviceId: String?,
        attachmentId: String? = nil,
        sequence: Int? = nil,
        last: Bool? = nil,
        cameraName: String? = nil,
        luminance: Double? = nil,
        labels: [String]? = nil,
        ocrText: String? = nil,
        faceCount: Int? = nil,
        personCount: Int? = nil,
        lighting: String? = nil,
        colors: [String]? = nil,
        savedPath: String? = nil,
        mediaKind: String? = nil,
        clipDurationMs: Int? = nil,
        hasClip: Bool? = nil,
        clipSupported: Bool? = nil,
        capturedAtMs: Int? = nil,
        streaming: Bool? = nil,
        motionScore: Double? = nil,
        placeHint: String? = nil
    ) {
        sendLookFrame(
            requestId: requestId, jpeg: jpeg, width: width, height: height,
            error: error, permission: permission, deviceId: deviceId,
            attachmentId: attachmentId, sequence: sequence, last: last,
            cameraName: cameraName, luminance: luminance, labels: labels,
            ocrText: ocrText, faceCount: faceCount, personCount: personCount,
            lighting: lighting, colors: colors, savedPath: savedPath,
            mediaKind: mediaKind, clipDurationMs: clipDurationMs, hasClip: hasClip,
            clipSupported: clipSupported, capturedAtMs: capturedAtMs,
            streaming: streaming, motionScore: motionScore, placeHint: placeHint
        )
    }
}

/// Ticket-authenticated live voice WebSocket over the Mac relay.
///
/// Connects to the exact URL minted by `EvieGatewayLiveTicket.socketURL`
/// (relay base + server-provided `ws_path`, `?session_id=&ticket=`). Sends
/// mirror `LiveVoiceConnection` framing (controls first, playback coalesced,
/// PCM FIFO capped at ~2s) and inbound frames decode to `LiveVoiceEvent`
/// with the same field mapping, so the coordinator drives both channels
/// identically.
public final class RelayLiveVoiceConnection: LiveVoiceChannel, @unchecked Sendable {
    private enum PendingMessage: Sendable {
        case audio(Data)
        case text(String)
    }

    private let socketURL: URL
    private let session: URLSession
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var lastInboundNs: UInt64 = 0
    private var lastOutboundNs: UInt64 = 0
    private var streamContinuation: AsyncThrowingStream<LiveVoiceEvent, Error>.Continuation?
    private var senderTask: Task<Void, Never>?
    private var senderContinuation: AsyncStream<Void>.Continuation?
    private var pendingAudio: Data?
    private var pendingControlMessages: [String] = []
    private var pendingPlaybackMessage: String?
    private var sendGeneration = 0
    private var keepaliveAckSeen = false
    private let lock = NSLock()

    /// - Parameter socketURL: prebuilt `ticket.socketURL(baseURL: relayBase)`.
    public init(socketURL: URL, session: URLSession = EVAPIClient.liveSocketSession) {
        self.socketURL = socketURL
        self.session = session
    }

    /// `sessionId` is already bound into `socketURL`; kept for
    /// `LiveVoiceChannel` parity with the direct path.
    public func connect(sessionId: String) async throws -> AsyncThrowingStream<LiveVoiceEvent, Error> {
        close()
        let request = URLRequest(url: socketURL)
        let task = session.webSocketTask(with: request)
        socket = task
        startSender(for: task)
        let stream = AsyncThrowingStream<LiveVoiceEvent, Error> { continuation in
            self.streamContinuation = continuation
            continuation.onTermination = { [weak self] _ in
                self?.close()
            }
        }
        task.resume()
        lock.lock()
        let receiveGeneration = sendGeneration
        let now = DispatchTime.now().uptimeNanoseconds
        lastInboundNs = now
        lastOutboundNs = now
        keepaliveAckSeen = false
        lock.unlock()
        receiveTask = Task.detached(priority: .userInitiated) { [weak self] in
            await self?.receiveLoop(task, generation: receiveGeneration)
        }
        startPingWatchdog(for: task, generation: receiveGeneration)
        return stream
    }

    public func enqueuePCM(_ data: Data) {
        guard !data.isEmpty else { return }
        queue(.audio(data))
    }

    public func sendControl(_ action: String) {
        sendJSON(["type": "control", "action": action])
    }

    public func sendCamera(_ state: CameraState, deviceId: String?) {
        var payload: [String: Any] = ["type": "camera", "action": state.rawValue]
        if let deviceId, !deviceId.isEmpty { payload["device_id"] = deviceId }
        sendJSON(payload)
    }

    public func sendLookFrame(
        requestId: String?,
        jpeg: Data?,
        width: Int?,
        height: Int?,
        error: String?,
        permission: String?,
        deviceId: String?,
        attachmentId: String? = nil,
        sequence: Int? = nil,
        last: Bool? = nil,
        cameraName: String? = nil,
        luminance: Double? = nil,
        labels: [String]? = nil,
        ocrText: String? = nil,
        faceCount: Int? = nil,
        personCount: Int? = nil,
        lighting: String? = nil,
        colors: [String]? = nil,
        savedPath: String? = nil,
        mediaKind: String? = nil,
        clipDurationMs: Int? = nil,
        hasClip: Bool? = nil,
        clipSupported: Bool? = nil,
        capturedAtMs: Int? = nil,
        streaming: Bool? = nil,
        motionScore: Double? = nil,
        placeHint: String? = nil
    ) {
        var payload: [String: Any] = ["type": "look_frame", "explicit_request": true]
        if let requestId, !requestId.isEmpty { payload["request_id"] = requestId }
        if let attachmentId { payload["attachment_id"] = attachmentId }
        if let jpeg, !jpeg.isEmpty { payload["jpeg_b64"] = jpeg.base64EncodedString() }
        if let width { payload["width"] = width }
        if let height { payload["height"] = height }
        if let error, !error.isEmpty { payload["error"] = error }
        if let permission, !permission.isEmpty { payload["permission"] = permission }
        if let deviceId, !deviceId.isEmpty { payload["device_id"] = deviceId }
        if let sequence { payload["sequence"] = sequence }
        if let last { payload["last"] = last }
        if let cameraName, !cameraName.isEmpty { payload["camera_name"] = cameraName }
        if let luminance { payload["luminance"] = luminance }
        if let labels, !labels.isEmpty { payload["labels"] = labels }
        if let ocrText, !ocrText.isEmpty { payload["ocr_text"] = ocrText }
        if let faceCount { payload["face_count"] = faceCount }
        if let personCount { payload["person_count"] = personCount }
        if let lighting, !lighting.isEmpty { payload["lighting"] = lighting }
        if let colors, !colors.isEmpty { payload["colors"] = colors }
        if let savedPath, !savedPath.isEmpty { payload["saved_path"] = savedPath }
        if let mediaKind, !mediaKind.isEmpty { payload["media_kind"] = mediaKind }
        if let clipDurationMs { payload["duration_ms"] = clipDurationMs }
        if let hasClip { payload["has_clip"] = hasClip }
        if let clipSupported { payload["clip_supported"] = clipSupported }
        if let capturedAtMs { payload["captured_at_ms"] = capturedAtMs }
        if let streaming {
            payload["streaming"] = streaming
            payload["is_stream"] = streaming
        }
        if let motionScore { payload["motion_score"] = motionScore }
        if let placeHint, !placeHint.isEmpty { payload["place_hint"] = placeHint }
        sendJSON(payload)
    }

    public func sendPlayback(active: Bool) {
        sendJSON(["type": "playback", "active": active])
    }

    public func close() {
        receiveTask?.cancel()
        receiveTask = nil
        pingTask?.cancel()
        pingTask = nil
        lock.lock()
        sendGeneration += 1
        let task = socket
        socket = nil
        senderContinuation?.finish()
        senderContinuation = nil
        senderTask?.cancel()
        senderTask = nil
        let continuation = streamContinuation
        streamContinuation = nil
        lock.unlock()
        task?.cancel(with: .goingAway, reason: nil)
        continuation?.finish()
    }

    private func sendJSON(_ object: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(object) else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        queue(.text(String(decoding: data, as: UTF8.self)),
              coalescePlayback: object["type"] as? String == "playback")
    }

    private func queue(_ message: PendingMessage, coalescePlayback: Bool = false) {
        lock.lock()
        guard socket != nil else {
            lock.unlock()
            return
        }
        switch message {
        case .audio(let data):
            var pending = pendingAudio ?? Data()
            pending.append(data)
            let maxBytes = 16_000 * 2 * 2
            if pending.count > maxBytes {
                var drop = pending.count - maxBytes
                if drop % 2 != 0 { drop += 1 }
                pending.removeFirst(min(drop, pending.count))
            }
            pendingAudio = pending.isEmpty ? nil : pending
        case .text(let text):
            if coalescePlayback {
                pendingPlaybackMessage = text
            } else {
                pendingControlMessages.append(text)
            }
        }
        let signal = senderContinuation
        lock.unlock()
        signal?.yield(())
    }

    private func startSender(for task: URLSessionWebSocketTask) {
        let signals = AsyncStream<Void>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            self.lock.lock()
            self.senderContinuation = continuation
            self.lock.unlock()
        }
        lock.lock()
        let generation = sendGeneration
        senderTask = Task.detached(priority: .userInitiated) { [weak self] in
            for await _ in signals {
                guard let self else { return }
                while let message = self.nextMessage(for: task, generation: generation) {
                    do {
                        try await task.send(message)
                        self.noteOutbound()
                    } catch {
                        self.failDeadLink(task, generation: generation)
                        return
                    }
                }
            }
        }
        lock.unlock()
    }

    private func nextMessage(
        for task: URLSessionWebSocketTask,
        generation: Int
    ) -> URLSessionWebSocketTask.Message? {
        lock.lock()
        defer { lock.unlock() }
        guard sendGeneration == generation, socket === task else { return nil }
        if !pendingControlMessages.isEmpty {
            return .string(pendingControlMessages.removeFirst())
        }
        if let pendingPlaybackMessage {
            self.pendingPlaybackMessage = nil
            return .string(pendingPlaybackMessage)
        }
        if let pendingAudio {
            self.pendingAudio = nil
            return .data(pendingAudio)
        }
        return nil
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask, generation: Int) async {
        do {
            while !Task.isCancelled {
                guard isCurrent(task, generation: generation) else { return }
                let message = try await task.receive()
                noteInbound()
                guard isCurrent(task, generation: generation) else { return }
                let data: Data
                switch message {
                case .data(let value):
                    data = value
                case .string(let value):
                    data = Data(value.utf8)
                @unknown default:
                    continue
                }
                if let event = Self.decode(data) {
                    guard isCurrent(task, generation: generation) else { return }
                    streamContinuation?.yield(event)
                    if event.fatal { break }
                }
            }
            streamContinuation?.finish()
        } catch is CancellationError {
            streamContinuation?.finish()
        } catch {
            streamContinuation?.finish(throwing: error)
        }
    }

    private func isCurrent(_ task: URLSessionWebSocketTask, generation: Int) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return sendGeneration == generation && socket === task
    }

    private func startPingWatchdog(for task: URLSessionWebSocketTask, generation: Int) {
        pingTask?.cancel()
        pingTask = Task.detached(priority: .utility) { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                guard let self, self.isCurrent(task, generation: generation) else { return }
                self.sendJSON(["type": "keepalive"])
                if self.linkLooksDead() {
                    self.failDeadLink(task, generation: generation)
                    return
                }
            }
        }
    }

    private func noteInbound() {
        lock.lock()
        lastInboundNs = DispatchTime.now().uptimeNanoseconds
        lock.unlock()
    }

    private func noteOutbound() {
        lock.lock()
        lastOutboundNs = DispatchTime.now().uptimeNanoseconds
        lock.unlock()
    }

    private func linkLooksDead() -> Bool {
        lock.lock()
        let inbound = lastInboundNs
        let outbound = lastOutboundNs
        let acked = keepaliveAckSeen
        lock.unlock()
        let now = DispatchTime.now().uptimeNanoseconds
        let inboundAge = now &- inbound
        let outboundAge = now &- outbound
        if acked {
            return inboundAge > 90_000_000_000
        }
        return inboundAge > 90_000_000_000 && outboundAge > 30_000_000_000
    }

    private func failDeadLink(_ task: URLSessionWebSocketTask, generation: Int) {
        guard isCurrent(task, generation: generation) else { return }
        receiveTask?.cancel()
        task.cancel(with: .goingAway, reason: nil)
    }

    private static func decode(_ data: Data) -> LiveVoiceEvent? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = object["type"] as? String
        else { return nil }
        if type == "keepalive" { return nil }
        let rawState = object["state"] as? [String: Any] ?? [:]
        let state = rawState.reduce(into: [String: String]()) {
            if let value = $1.value as? String {
                $0[$1.key] = value
            } else if let value = $1.value as? Bool {
                $0[$1.key] = value ? "true" : "false"
            } else if let value = $1.value as? NSNumber {
                $0[$1.key] = value.stringValue
            }
        }
        let configObject = object["config"] as? [String: Any] ?? [:]
        let config = AnyCodable.dictionary(configObject) ?? [:]
        let cameraRaw = object["camera_state"]
            ?? object["camera"]
            ?? object["camera_status"]
            ?? rawState["camera_state"]
            ?? rawState["camera"]
            ?? configObject["camera_state"]
            ?? configObject["camera"]
            ?? ((type == "camera_state" || type == "camera") ? object : nil)
        let capabilityRaw = configObject["capabilities"]
            ?? configObject["capability_manifest"]
            ?? configObject["capabilityManifest"]
            ?? object["capabilities"]
            ?? object["capability_manifest"]
            ?? object["capabilityManifest"]
            ?? rawState["capabilities"]
        let realtimeRaw = object["realtime_diagnostics"]
            ?? object["realtimeDiagnostics"]
            ?? object["diagnostics"]
            ?? rawState["realtime"]
            ?? configObject["realtime"]
        let deviceRaw = configObject["devices"] as? [Any]
            ?? object["devices"] as? [Any]
            ?? rawState["devices"] as? [Any]
            ?? []
        return LiveVoiceEvent(
            type: type,
            text: (object["text"] as? String) ?? (object["message"] as? String),
            audioB64: object["audio_b64"] as? String,
            audioRef: object["audio_ref"] as? String,
            state: state,
            sessionId: object["session_id"] as? String,
            conversationId: object["conversation_id"] as? String,
            code: object["code"] as? String,
            fatal: object["fatal"] as? Bool ?? false,
            metric: object["metric"] as? String,
            ms: intValue(object["ms"]),
            contentType: object["content_type"] as? String,
            sampleRate: intValue(object["sample_rate"]),
            providerResponseId: object["provider_response_id"] as? String,
            index: object["index"] as? Int,
            hud: decodeHUD(object["hud"] ?? object["card"]),
            deviceId: (object["device_id"] as? String) ?? (configObject["device_id"] as? String),
            ttsDeviceId: (object["tts_device_id"] as? String) ?? (configObject["tts_device_id"] as? String),
            config: config,
            cameraState: decodeCamera(cameraRaw),
            capabilityManifest: decodeCapabilities(capabilityRaw),
            realtimeDiagnostics: (realtimeRaw as? [String: Any]).flatMap(AnyCodable.dictionary),
            deviceMesh: deviceRaw.compactMap(decodeDevice),
            deviceMeshReported: configObject["devices"] != nil
                || object["devices"] != nil
                || rawState["devices"] != nil,
            action: object["action"] as? String,
            requestId: object["request_id"] as? String,
            durationMs: intValue(object["duration_ms"]),
            intervalMs: intValue(object["interval_ms"]),
            maxFrames: intValue(object["max_frames"]),
            detail: object["detail"] as? String,
            command: (object["command"] as? String) ?? (object["action"] as? String),
            arguments: AnyCodable.dictionary(stringKeyedDictionary(object["arguments"])) ?? [:],
            gesture: object["gesture"] as? String,
            intensity: object["intensity"] as? String,
            floor: object["floor"] as? String
        )
    }

    private static func intValue(_ raw: Any?) -> Int? {
        if let value = raw as? Int { return value }
        if let value = raw as? Double { return Int(value) }
        if let value = raw as? NSNumber { return value.intValue }
        return nil
    }

    private static func stringKeyedDictionary(_ raw: Any?) -> [String: Any]? {
        if let dict = raw as? [String: Any] { return dict }
        guard let dict = raw as? NSDictionary else { return nil }
        var out: [String: Any] = [:]
        out.reserveCapacity(dict.count)
        for (key, value) in dict {
            if let key = key as? String { out[key] = value }
        }
        return out
    }

    private static func decodeCamera(_ raw: Any?) -> CameraStateSnapshot? {
        if let object = raw as? [String: Any] { return CameraStateSnapshot(json: object) }
        if let state = raw as? String {
            return CameraStateSnapshot(state: CameraState(rawValue: state.lowercased()) ?? .unknown)
        }
        return nil
    }

    private static func decodeCapabilities(_ raw: Any?) -> CapabilityManifest? {
        if let object = raw as? [String: Any] { return CapabilityManifest(json: object) }
        if let values = raw as? [String] { return CapabilityManifest(enabled: values) }
        return nil
    }

    private static func decodeDevice(_ raw: Any) -> DeviceMeshNode? {
        guard let object = raw as? [String: Any] else { return nil }
        let id = object["device_id"] as? String ?? object["id"] as? String
        guard let id, !id.isEmpty else { return nil }
        return DeviceMeshNode(
            id: id,
            name: object["name"] as? String ?? id,
            presence: DevicePresence(rawValue: object["presence"] as? String ?? "unknown"),
            capabilities: object["capabilities"] as? [String] ?? [],
            deviceType: object["device_type"] as? String ?? object["deviceType"] as? String,
            platform: object["platform"] as? String,
            batteryPercent: object["battery_percent"] as? Double,
            lastSeenAt: object["last_seen_at"] as? String,
            lastHeartbeatAt: object["last_heartbeat_at"] as? String
        )
    }

    private static func decodeHUD(_ raw: Any?) -> HUDCard? {
        guard let object = raw as? [String: Any] else { return nil }
        let title = object["title"] as? String ?? ""
        let body = object["body"] as? String ?? ""
        guard !title.isEmpty || !body.isEmpty else { return nil }
        let priority: Double
        if let number = object["priority"] as? Double {
            priority = number
        } else if let number = object["priority"] as? Int {
            priority = Double(number)
        } else {
            priority = 0
        }
        return HUDCard(
            schemaVersion: object["schema_version"] as? String ?? HUDCard.schemaVersionV1,
            generatedAt: object["generated_at"] as? String ?? "",
            title: title,
            body: body,
            priority: priority,
            meta: AnyCodable.dictionary(object["meta"] as? [String: Any])
        )
    }
}
