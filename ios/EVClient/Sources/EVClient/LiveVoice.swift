import Foundation
#if os(iOS) || os(macOS)
import AVFoundation
import Darwin
#endif

/// One event from the full-duplex `WS /v1/voice/live` conversation channel.
public struct LiveVoiceEvent: Sendable {
    public let type: String
    public let text: String?
    public let audioB64: String?
    public let audioRef: String?
    public let state: [String: String]
    public let sessionId: String?
    public let conversationId: String?
    public let contentType: String?
    public let sampleRate: Int?
    public let code: String?
    public let fatal: Bool
    public let metric: String?
    public let ms: Int?
    public let hud: HUDCard?
    public let deviceId: String?
    public let ttsDeviceId: String?
    public let config: [String: AnyCodable]
    public let cameraState: CameraStateSnapshot?
    public let capabilityManifest: CapabilityManifest?
    public let realtimeDiagnostics: [String: AnyCodable]?
    public let deviceMesh: [DeviceMeshNode]
    public let deviceMeshReported: Bool
    public let action: String?
    public let requestId: String?
    public let durationMs: Int?
    public let intervalMs: Int?
    public let maxFrames: Int?
    public let detail: String?
    public let command: String?
    public let arguments: [String: AnyCodable]

    public init(
        type: String,
        text: String? = nil,
        audioB64: String? = nil,
        audioRef: String? = nil,
        state: [String: String] = [:],
        sessionId: String? = nil,
        conversationId: String? = nil,
        code: String? = nil,
        fatal: Bool = false,
        metric: String? = nil,
        ms: Int? = nil,
        contentType: String? = nil,
        sampleRate: Int? = nil,
        hud: HUDCard? = nil,
        deviceId: String? = nil,
        ttsDeviceId: String? = nil,
        config: [String: AnyCodable] = [:],
        cameraState: CameraStateSnapshot? = nil,
        capabilityManifest: CapabilityManifest? = nil,
        realtimeDiagnostics: [String: AnyCodable]? = nil,
        deviceMesh: [DeviceMeshNode] = [],
        deviceMeshReported: Bool = false,
        action: String? = nil,
        requestId: String? = nil,
        durationMs: Int? = nil,
        intervalMs: Int? = nil,
        maxFrames: Int? = nil,
        detail: String? = nil,
        command: String? = nil,
        arguments: [String: AnyCodable] = [:]
    ) {
        self.type = type
        self.text = text
        self.audioB64 = audioB64
        self.audioRef = audioRef
        self.state = state
        self.sessionId = sessionId
        self.conversationId = conversationId
        self.code = code
        self.fatal = fatal
        self.metric = metric
        self.ms = ms
        self.contentType = contentType
        self.sampleRate = sampleRate
        self.hud = hud
        self.deviceId = deviceId
        self.ttsDeviceId = ttsDeviceId
        self.config = config
        self.cameraState = cameraState
        self.capabilityManifest = capabilityManifest
        self.realtimeDiagnostics = realtimeDiagnostics
        self.deviceMesh = deviceMesh
        self.deviceMeshReported = deviceMeshReported
        self.action = action
        self.requestId = requestId
        self.durationMs = durationMs
        self.intervalMs = intervalMs
        self.maxFrames = maxFrames
        self.detail = detail
        self.command = command
        self.arguments = arguments
    }

    public var argumentObject: [String: Any] {
        arguments.mapValues { $0.jsonObject() }
    }
}

/// A client-side WebSocket for EV LIVE.
///
/// Audio is sent as raw 16 kHz mono PCM16 frames while the server sends state,
/// partial transcripts, backchannels, barge-in signals, and playable TTS
/// chunks on the same connection.
public final class LiveVoiceConnection: @unchecked Sendable {
    private enum PendingMessage: Sendable {
        case audio(Data)
        case text(String)
    }

    private let baseURL: URL
    private let token: String
    private let session: URLSession
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    /// Bounded dead-link detection: unanswered protocol pings. A backend
    /// that vanishes without a clean close must NEVER leave this client
    /// hanging forever on receive() (observed 2026-08-24 after a backend
    /// restart): two missed pongs terminate the stream so the normal
    /// reconnect loop owns recovery.
    private var pingTask: Task<Void, Never>?
    private var missedPongs = 0
    private var streamContinuation: AsyncThrowingStream<LiveVoiceEvent, Error>.Continuation?
    private var senderTask: Task<Void, Never>?
    private var senderContinuation: AsyncStream<Void>.Continuation?
    private var pendingAudio: Data?
    private var pendingControlMessages: [String] = []
    private var pendingPlaybackMessage: String?
    private var sendGeneration = 0
    private let lock = NSLock()

    public init(
        baseURL: URL,
        token: String,
        session: URLSession = EVAPIClient.voiceSession
    ) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    public func connect(
        sessionId: String
    ) async throws -> AsyncThrowingStream<LiveVoiceEvent, Error> {
        close()
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw EVAPIError.transport("invalid EV LIVE base URL")
        }
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.path = components.path
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            + "/v1/voice/live"
        components.queryItems = [
            URLQueryItem(name: "session_id", value: sessionId),
            URLQueryItem(name: "token", value: token),
        ]
        guard let url = components.url else {
            throw EVAPIError.transport("invalid EV LIVE WebSocket URL")
        }

        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
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
        lock.unlock()
        receiveTask = Task.detached(priority: .userInitiated) { [weak self] in
            await self?.receiveLoop(task, generation: receiveGeneration)
        }
        startPingWatchdog(for: task, generation: receiveGeneration)
        return stream
    }

    /// Bounded keepalive: ping every 15s; two consecutive unanswered pongs
    /// (~30s) declare the link dead and hand recovery to the reconnect loop.
    private func startPingWatchdog(for task: URLSessionWebSocketTask, generation: Int) {
        pingTask?.cancel()
        lock.lock()
        missedPongs = 0
        lock.unlock()
        pingTask = Task.detached(priority: .utility) { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard let self, self.isCurrent(task, generation: generation) else { return }
                let strikes = self.registerPingSent()
                if strikes >= 2 {
                    self.failDeadLink(task, generation: generation)
                    return
                }
                task.sendPing { [weak self] _ in
                    self?.clearMissedPongs()
                }
            }
        }
    }

    private func registerPingSent() -> Int {
        lock.lock()
        defer { lock.unlock() }
        missedPongs += 1
        return missedPongs
    }

    private func clearMissedPongs() {
        lock.lock()
        missedPongs = 0
        lock.unlock()
    }

    /// Dead link: cancel the blocked receive and tear the socket down so the
    /// LiveConversation loop performs its normal bounded reconnect.
    private func failDeadLink(_ task: URLSessionWebSocketTask, generation: Int) {
        guard isCurrent(task, generation: generation) else { return }
        receiveTask?.cancel()
        task.cancel(with: .goingAway, reason: nil)
    }

    /// Keep only the newest unsent PCM frame. A microphone callback must never
    /// create a stale FIFO backlog that makes the next user turn arrive late.
    public func enqueuePCM(_ data: Data) {
        guard !data.isEmpty else { return }
        queue(.audio(data))
    }

    public func sendControl(_ action: String) {
        sendControl(action, deviceId: nil)
    }

    /// Send a control to a named mesh node without changing the current
    /// audio owner. Older servers safely ignore the optional target.
    public func sendControl(_ action: String, deviceId: String?) {
        sendControl(action, deviceId: deviceId, extra: nil)
    }

    /// Optional extra fields (barge-in timing, etc). Unknown keys are ignored
    /// by older servers. This is the live socket control path, not UI.
    public func sendControl(_ action: String, extra: [String: Any]) {
        sendControl(action, deviceId: nil, extra: extra)
    }

    public func sendControl(_ action: String, deviceId: String?, extra: [String: Any]?) {
        var payload: [String: Any] = ["type": "control", "action": action]
        if let deviceId, !deviceId.isEmpty {
            payload["device_id"] = deviceId
        }
        if let extra {
            for (key, value) in extra {
                guard key != "type", key != "action" else { continue }
                payload[key] = value
            }
        }
        sendJSON(payload)
    }

    /// Camera requests are explicit and target-bound. Camera state is updated
    /// only after a provider sends a camera-state event.
    public func sendCamera(_ state: CameraState, deviceId: String?) {
        var payload: [String: Any] = [
            "type": "camera",
            "action": state.rawValue,
            "explicit_request": true,
        ]
        if let deviceId, !deviceId.isEmpty {
            payload["device_id"] = deviceId
        }
        sendJSON(payload)
    }

    public func sendLookFrame(attachmentId: String, deviceId: String?) {
        sendLookFrame(
            requestId: nil,
            jpeg: nil,
            width: nil,
            height: nil,
            error: attachmentId.isEmpty ? "empty_frame" : nil,
            permission: nil,
            deviceId: deviceId,
            attachmentId: attachmentId
        )
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
        cameraName: String? = nil
    ) {
        var payload: [String: Any] = [
            "type": "look_frame",
            "explicit_request": true,
        ]
        if let requestId, !requestId.isEmpty {
            payload["request_id"] = requestId
        }
        if let attachmentId {
            payload["attachment_id"] = attachmentId
        }
        if let jpeg, !jpeg.isEmpty {
            payload["jpeg_b64"] = jpeg.base64EncodedString()
        }
        if let width { payload["width"] = width }
        if let height { payload["height"] = height }
        if let error, !error.isEmpty { payload["error"] = error }
        if let permission, !permission.isEmpty { payload["permission"] = permission }
        if let deviceId, !deviceId.isEmpty { payload["device_id"] = deviceId }
        if let sequence { payload["sequence"] = sequence }
        if let last { payload["last"] = last }
        if let cameraName, !cameraName.isEmpty { payload["camera_name"] = cameraName }
        sendJSON(payload)
    }

    public func sendCameraReadiness(
        permission: String,
        deviceId: String?,
        cameraName: String?
    ) {
        var state: [String: Any] = [
            "state": permission == "denied" ? "denied" : "off",
            "visible": false,
            "permission_state": permission,
            "platform": "macos",
            "raw_frames_persisted": false,
            "explicit_request": false,
        ]
        if let deviceId, !deviceId.isEmpty { state["device_id"] = deviceId }
        if let cameraName, !cameraName.isEmpty { state["camera_name"] = cameraName }
        sendJSON(["type": "camera_state", "camera_state": state])
    }

    public func sendComputerState(_ state: [String: Any], deviceId: String?) {
        var payload: [String: Any] = [
            "type": "computer_state",
            "computer_state": state,
        ]
        if let deviceId, !deviceId.isEmpty {
            payload["device_id"] = deviceId
        }
        sendJSON(payload)
    }

    public func sendComputerResult(
        requestId: String?,
        command: String?,
        result: [String: Any],
        jpeg: Data? = nil,
        deviceId: String? = nil
    ) {
        var payload = Self.jsonSafeDictionary(result)
        payload["type"] = "computer_result"
        if let requestId, !requestId.isEmpty {
            payload["request_id"] = requestId
        }
        if let command, !command.isEmpty {
            payload["command"] = command
        }
        if let jpeg, !jpeg.isEmpty {
            payload["jpeg_b64"] = jpeg.base64EncodedString()
            payload.removeValue(forKey: "jpeg")
        }
        if let deviceId, !deviceId.isEmpty {
            payload["device_id"] = deviceId
        }
        sendJSON(payload)
    }

    public func sendPlayback(active: Bool) {
        sendJSON(["type": "playback", "active": active])
    }

    public func sendText(_ text: String, commit: Bool = true) {
        sendJSON(["type": "text", "text": text, "commit": commit])
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
        pendingAudio = nil
        pendingControlMessages.removeAll(keepingCapacity: false)
        pendingPlaybackMessage = nil
        lock.unlock()
        task?.cancel(with: .normalClosure, reason: nil)
        streamContinuation?.finish()
        streamContinuation = nil
    }

    private func sendJSON(_ object: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(object) else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        queue(.text(String(decoding: data, as: UTF8.self)),
              coalescePlayback: object["type"] as? String == "playback")
    }

    private static func jsonSafeDictionary(_ raw: [String: Any]) -> [String: Any] {
        var out: [String: Any] = [:]
        for (key, value) in raw {
            if let safe = jsonSafe(value) {
                out[key] = safe
            }
        }
        return out
    }

    private static func jsonSafe(_ value: Any) -> Any? {
        switch value {
        case is NSNull, is String, is Bool:
            return value
        case let number as Int:
            return number
        case let number as Double:
            return number
        case let number as Float:
            return Double(number)
        case let number as Int32:
            return Int(number)
        case let number as UInt32:
            return Int(number)
        case let number as Int64:
            return Int(number)
        case let number as NSNumber:
            return number
        case let data as Data:
            return data.base64EncodedString()
        case let array as [Any]:
            return array.compactMap { jsonSafe($0) }
        case let dict as [String: Any]:
            return jsonSafeDictionary(dict)
        default:
            return String(describing: value)
        }
    }

    private func queue(_ message: PendingMessage, coalescePlayback: Bool = false) {
        lock.lock()
        guard socket != nil else {
            lock.unlock()
            return
        }
        switch message {
        case .audio(let data):
            pendingAudio = data
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
                    } catch {
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
        guard sendGeneration == generation, socket === task else {
            return nil
        }
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
                if let event = decode(data) {
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

    private func decode(_ data: Data) -> LiveVoiceEvent? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = object["type"] as? String
        else { return nil }
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
        let msValue: Int? = Self.intValue(object["ms"])
        let sampleRate: Int?
        if let number = object["sample_rate"] as? Int {
            sampleRate = number
        } else if let number = object["sample_rate"] as? Double {
            sampleRate = Int(number)
        } else {
            sampleRate = nil
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
        let cameraState = Self.decodeCamera(cameraRaw)
        let capabilityRaw = configObject["capabilities"]
            ?? configObject["capability_manifest"]
            ?? configObject["capabilityManifest"]
            ?? object["capabilities"]
            ?? object["capability_manifest"]
            ?? object["capabilityManifest"]
            ?? rawState["capabilities"]
        let capabilityManifest = Self.decodeCapabilities(capabilityRaw)
        let realtimeRaw = object["realtime_diagnostics"]
            ?? object["realtimeDiagnostics"]
            ?? object["diagnostics"]
            ?? rawState["realtime"]
            ?? configObject["realtime"]
        let realtimeDiagnostics = (realtimeRaw as? [String: Any]).flatMap(AnyCodable.dictionary)
        let deviceRaw = configObject["devices"] as? [Any]
            ?? object["devices"] as? [Any]
            ?? rawState["devices"] as? [Any]
            ?? []
        let deviceMesh = deviceRaw.compactMap(Self.decodeDevice)
        let deviceMeshReported = configObject["devices"] != nil
            || object["devices"] != nil
            || rawState["devices"] != nil
        let eventDeviceId = object["device_id"] as? String
            ?? configObject["device_id"] as? String
        let eventTTSDeviceId = object["tts_device_id"] as? String
            ?? configObject["tts_device_id"] as? String
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
            ms: msValue,
            contentType: object["content_type"] as? String,
            sampleRate: sampleRate,
            hud: Self.decodeHUD(object["hud"] ?? object["card"]),
            deviceId: eventDeviceId,
            ttsDeviceId: eventTTSDeviceId,
            config: config,
            cameraState: cameraState,
            capabilityManifest: capabilityManifest,
            realtimeDiagnostics: realtimeDiagnostics,
            deviceMesh: deviceMesh,
            deviceMeshReported: deviceMeshReported,
            action: object["action"] as? String,
            requestId: object["request_id"] as? String,
            durationMs: Self.intValue(object["duration_ms"]),
            intervalMs: Self.intValue(object["interval_ms"]),
            maxFrames: Self.intValue(object["max_frames"]),
            detail: object["detail"] as? String,
            command: object["command"] as? String ?? object["action"] as? String,
            arguments: AnyCodable.dictionary(object["arguments"] as? [String: Any]) ?? [:]
        )
    }

    private static func intValue(_ raw: Any?) -> Int? {
        if let value = raw as? Int { return value }
        if let value = raw as? Double { return Int(value) }
        if let value = raw as? NSNumber { return value.intValue }
        return nil
    }

    private static func decodeCamera(_ raw: Any?) -> CameraStateSnapshot? {
        if let object = raw as? [String: Any] {
            return CameraStateSnapshot(json: object)
        }
        if let state = raw as? String {
            return CameraStateSnapshot(
                state: CameraState(rawValue: state.lowercased()) ?? .unknown
            )
        }
        return nil
    }

    private static func decodeCapabilities(_ raw: Any?) -> CapabilityManifest? {
        if let object = raw as? [String: Any] {
            return CapabilityManifest(json: object)
        }
        if let values = raw as? [String] {
            return CapabilityManifest(enabled: values)
        }
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

#if os(iOS) || os(macOS)
/// Captures microphone audio in the format consumed by EV LIVE.
///
/// The audio engine may run at the device's native rate. Conversion happens
/// before frames enter the WebSocket so the server receives a stable 16 kHz,
/// mono, PCM16 stream regardless of hardware.
///
/// Do not connect ``inputNode`` into the mixer. A tap already pulls input;
/// wiring input into the graph as well throws `kAudioUnitErr_CannotDoInCurrentContext`
/// (-10867) and shows up as “Microphone capture failed”.
public final class LiveVoiceMicrophone: @unchecked Sendable {
    public let sampleRate: Double = 16_000

    /// Shared with the TTS player so live duplex uses one AVAudioEngine.
    /// Attach playback nodes through ``start(enqueue:configure:)`` *before*
    /// the engine starts — mutating a running graph also throws -10867.
    public private(set) var engine = AVAudioEngine()

    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var running = false
    private var wanted = false
    private var starting = false
    private var tapInstalled = false
    private var enqueue: (@Sendable (Data) -> Void)?

    public init() {}

    /// Starts capture and forwards each converted frame to the live socket.
    public func start(
        enqueue: @escaping @Sendable (Data) -> Void,
        configure: ((AVAudioEngine) throws -> Void)? = nil
    ) throws {
        do {
            try startOnce(enqueue: enqueue, configure: configure)
        } catch {
            guard nsCode(error) == -10867 else { throw error }
            stop(clearWanted: false)
            // Player node on the capture graph can fail on some HAL states.
            // Capture-only still unmutes; TTS then uses its private engine.
            try startOnce(enqueue: enqueue, configure: nil)
        }
    }

    private func startOnce(
        enqueue: @escaping @Sendable (Data) -> Void,
        configure: ((AVAudioEngine) throws -> Void)?
    ) throws {
        stop(clearWanted: false)
        starting = true
        defer { starting = false }
        self.enqueue = enqueue
        wanted = true

        #if os(iOS)
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.allowBluetooth, .defaultToSpeaker]
        )
        try audioSession.setActive(true)
        #endif

        // Recreate after a just-accepted grant. An engine allocated before
        // Allow reports 0 Hz / 0 ch; installTap on that format aborts.
        engine = AVAudioEngine()
        engine.isAutoShutdownEnabled = false


        let inputFormat: AVAudioFormat
        do {
            inputFormat = try AVAudioSafe.attachAndPrepare(engine)
        } catch {
            throw error
        }
        let input = engine.inputNode
        // Do not enable AVAudio voice processing here. On a menu-bar
        // accessory it aborts the process when playback starts on a
        // second audio unit (Talk / TTS), which looked like the app quit.
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw EVAPIError.transport("microphone input format is unavailable")
        }
        // Tap format must be the node's output format. inputFormatForBus can
        // disagree with the HAL after prepare() and start() then returns -10867.
        let tapFormat = input.outputFormat(forBus: 0)
        let hwFormat = (tapFormat.sampleRate > 0 && tapFormat.channelCount > 0) ? tapFormat : inputFormat
        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw EVAPIError.transport("unable to create EV LIVE audio format")
        }
        guard let converter = AVAudioConverter(from: hwFormat, to: outputFormat) else {
            throw EVAPIError.transport("unable to create EV LIVE audio converter")
        }
        self.converter = converter

        let ratio = sampleRate / max(hwFormat.sampleRate, 1)
        do {
            try AVAudioSafe.installTap(on: input, bufferSize: 1024, format: hwFormat) { [weak self] buffer, _ in
                guard let self else { return }
                self.lock.lock()
                defer { self.lock.unlock() }
                guard self.running, let converter = self.converter else { return }
                let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1
                guard let converted = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
                    return
                }
                var conversionError: NSError?
                var supplied = false
                let status = converter.convert(to: converted, error: &conversionError) { _, outStatus in
                    if supplied {
                        outStatus.pointee = .noDataNow
                        return nil
                    }
                    supplied = true
                    outStatus.pointee = .haveData
                    return buffer
                }
                guard converted.frameLength > 0,
                      status == .haveData || status == .inputRanDry,
                      let channel = converted.int16ChannelData?.pointee else {
                    return
                }
                let data = Data(bytes: channel, count: Int(converted.frameLength) * MemoryLayout<Int16>.size)
                enqueue(data)
            }
        } catch {
            self.converter = nil
            throw error
        }
        tapInstalled = true

        do {
            try configure?(engine)
        } catch {
            removeTapIfNeeded()
            self.converter = nil
            throw error
        }

        do {
            lock.lock()
            running = true
            lock.unlock()
            try AVAudioSafe.start(engine)
        } catch {
            lock.lock()
            running = false
            lock.unlock()
            removeTapIfNeeded()
            self.converter = nil
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setActive(false)
            #endif
            throw error
        }
    }

    public func stop() {
        stop(clearWanted: true)
    }

    /// Rebuild the capture graph after an audio-route/interruption recovery.
    /// It preserves the last enqueue destination and never reports success
    /// unless the engine starts again.
    public func recover() throws {
        lock.lock()
        let shouldRecover = wanted
        let callback = enqueue
        lock.unlock()
        guard shouldRecover, let callback else {
            throw EVAPIError.transport("microphone recovery is not requested")
        }
        try start(enqueue: callback)
    }

    private func stop(clearWanted: Bool) {
        lock.lock()
        if clearWanted {
            wanted = false
        }
        let wasRunning = running
        running = false
        lock.unlock()
        guard wasRunning || converter != nil || tapInstalled else { return }
        removeTapIfNeeded()
        AVAudioSafe.stop(engine)
        converter = nil
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    private func removeTapIfNeeded() {
        guard tapInstalled else { return }
        AVAudioSafe.removeTap(on: engine.inputNode)
        tapInstalled = false
    }

    private func nsCode(_ error: Error) -> Int {
        (error as NSError).code
    }

    deinit {
        stop()
    }
}

/// Plays 16 kHz mono PCM16 returned on the live socket.
///
/// iOS live duplex uses this engine separately from capture. Mac live
/// playback stays on the existing menu-bar `TTSPlayer`.
public final class LivePCMPlayer: NSObject, AVAudioPlayerDelegate, @unchecked Sendable {
    public var onPlayingChange: ((Bool) -> Void)?
    public var onError: ((String) -> Void)?
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private let lock = NSLock()
    private var attached = false
    private var format: AVAudioFormat?
    private var pendingBuffers = 0
    private var pendingFrames = 0
    private var generation = 0
    private var startTask: DispatchWorkItem?
    private var startDeadline = Date.distantPast
    private var captureMuteUntil = Date.distantPast
    private let minStartSeconds: Double = 0.04
    private let startDelay: TimeInterval = 0.02
    private let startRetryDelay: TimeInterval = 0.01
    private let captureEchoTail: TimeInterval = 0.16
    private var filePlayer: AVAudioPlayer?
    private var fileQueue: [Data] = []
    private var observers: [NSObjectProtocol] = []

    public override init() {
        super.init()
#if os(iOS)
        let center = NotificationCenter.default
        for name in [
            AVAudioSession.routeChangeNotification,
            AVAudioSession.interruptionNotification,
            AVAudioSession.mediaServicesWereResetNotification,
        ] {
            observers.append(
                center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                    self?.stop()
                }
            )
        }
#endif
    }

    deinit {
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    public var shouldMuteCapture: Bool {
        lock.lock()
        let active = pendingBuffers > 0
            || filePlayer?.isPlaying == true
            || !fileQueue.isEmpty
            || Date() < captureMuteUntil
        lock.unlock()
        return active
    }

    public func enqueue(
        _ data: Data,
        sampleRate: Double = 16_000,
        contentType: String? = nil
    ) {
        guard !data.isEmpty else { return }
        if let wav = Self.wavPCM(data) {
            enqueue(wav.samples, sampleRate: wav.sampleRate, contentType: "audio/pcm")
            return
        }
        guard Self.isPCM(data, contentType: contentType) else {
            enqueueFile(data)
            return
        }
        lock.lock()
        let hadPlayback = pendingBuffers > 0
        guard let format = ensureStarted(sampleRate: sampleRate) else {
            lock.unlock()
            if hadPlayback {
                onPlayingChange?(false)
            }
            onError?("audio output could not start")
            return
        }
        let frames = AVAudioFrameCount(data.count / MemoryLayout<Int16>.size)
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)
        else {
            lock.unlock()
            return
        }
        buffer.frameLength = frames
        if let dst = buffer.int16ChannelData?.pointee {
            data.withUnsafeBytes { raw in
                guard let src = raw.baseAddress else { return }
                memcpy(dst, src, min(data.count, Int(frames) * MemoryLayout<Int16>.size))
            }
        }
        pendingBuffers += 1
        pendingFrames += Int(frames)
        let scheduledGeneration = generation
        let first = pendingBuffers == 1
        let queuedSeconds = Double(pendingFrames) / format.sampleRate
        let shouldStartNow = !node.isPlaying
            && startTask == nil
            && queuedSeconds >= minStartSeconds
        let shouldPrime = !node.isPlaying && startTask == nil && !shouldStartNow
        if shouldPrime {
            startDeadline = Date().addingTimeInterval(0.12)
        }
        lock.unlock()
        if first {
            onPlayingChange?(true)
        }
        node.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.lock.lock()
            guard self.generation == scheduledGeneration else {
                self.lock.unlock()
                return
            }
            self.pendingBuffers = max(0, self.pendingBuffers - 1)
            self.pendingFrames = max(0, self.pendingFrames - Int(frames))
            let idle = self.pendingBuffers == 0
            if idle {
                self.captureMuteUntil = Date().addingTimeInterval(self.captureEchoTail)
            }
            self.lock.unlock()
            if idle {
                DispatchQueue.main.async {
                    self.lock.lock()
                    let stillIdle = self.generation == scheduledGeneration && self.pendingBuffers == 0
                    self.lock.unlock()
                    if stillIdle {
                        self.onPlayingChange?(false)
                    }
                }
            }
        }
        if shouldStartNow {
            node.play()
        } else if shouldPrime {
            primePlayback(generation: scheduledGeneration)
        }
    }

    public func stop() {
        lock.lock()
        let wasFilePlaying = filePlayer?.isPlaying == true || !fileQueue.isEmpty
        startTask?.cancel()
        startTask = nil
        startDeadline = .distantPast
        let wasPlaying = pendingBuffers > 0 || node.isPlaying
        generation += 1
        pendingBuffers = 0
        pendingFrames = 0
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        fileQueue.removeAll(keepingCapacity: false)
        filePlayer?.delegate = nil
        filePlayer?.stop()
        filePlayer = nil
        node.stop()
        node.reset()
        if engine.isRunning {
            engine.stop()
        }
        if attached {
            engine.disconnectNodeOutput(node)
            engine.detach(node)
        }
        attached = false
        format = nil
        lock.unlock()
        if wasPlaying || wasFilePlaying {
            onPlayingChange?(false)
        }
    }

    private func enqueueFile(_ data: Data) {
        lock.lock()
        if filePlayer?.isPlaying == true {
            fileQueue.append(data)
            lock.unlock()
            return
        }
        stopPCMLocked()
        do {
            let next = try AVAudioPlayer(data: data)
            next.delegate = self
            next.prepareToPlay()
            filePlayer = next
            let started = next.play()
            lock.unlock()
            if started {
                onPlayingChange?(true)
            } else {
                onError?("audio file playback could not start")
            }
        } catch {
            lock.unlock()
            onError?("audio file playback failed: \(error.localizedDescription)")
        }
    }

    private func stopPCMLocked() {
        startTask?.cancel()
        startTask = nil
        startDeadline = .distantPast
        generation += 1
        pendingBuffers = 0
        pendingFrames = 0
        node.stop()
        node.reset()
    }

    public func audioPlayerDidFinishPlaying(
        _ player: AVAudioPlayer,
        successfully flag: Bool
    ) {
        lock.lock()
        guard filePlayer === player else {
            lock.unlock()
            return
        }
        if !fileQueue.isEmpty {
            let data = fileQueue.removeFirst()
            do {
                let next = try AVAudioPlayer(data: data)
                next.delegate = self
                next.prepareToPlay()
                filePlayer = next
                let started = next.play()
                lock.unlock()
                if !started {
                    onError?("audio file playback could not continue")
                }
            } catch {
                filePlayer = nil
                lock.unlock()
                onError?("audio file playback failed: \(error.localizedDescription)")
            }
            return
        }
        filePlayer = nil
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        lock.unlock()
        onPlayingChange?(false)
    }

    private func primePlayback(generation: Int) {
        primePlayback(generation: generation, after: startDelay)
    }

    private func primePlayback(generation scheduledGeneration: Int, after delay: TimeInterval) {
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard self.generation == scheduledGeneration else {
                self.lock.unlock()
                return
            }
            let hasAudio = self.pendingBuffers > 0
            let queuedSeconds: Double
            if let format = self.format {
                queuedSeconds = Double(self.pendingFrames) / format.sampleRate
            } else {
                queuedSeconds = 0
            }
            let deadline = self.startDeadline
            self.startTask = nil
            self.lock.unlock()
            guard hasAudio, !self.node.isPlaying else { return }
            if queuedSeconds < self.minStartSeconds, Date() < deadline {
                self.primePlayback(
                    generation: scheduledGeneration,
                    after: self.startRetryDelay
                )
                return
            }
            self.node.play()
        }
        lock.lock()
        startTask = work
        lock.unlock()
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private static func isPCM(_ data: Data, contentType: String?) -> Bool {
        let kind = (contentType ?? "").lowercased()
        if kind.contains("pcm") || kind.contains("l16") { return true }
        if kind.contains("wav") || data.starts(with: Data("RIFF".utf8)) { return false }
        if kind.contains("mpeg") || kind.contains("mp3") || kind.contains("aac") {
            return false
        }
        if data.starts(with: Data("ID3".utf8)) { return false }
        return true
    }

    private struct WavPCM {
        let samples: Data
        let sampleRate: Double
    }

    private static func wavPCM(_ data: Data) -> WavPCM? {
        guard data.count >= 44, data.starts(with: Data("RIFF".utf8)) else { return nil }
        var offset = 12
        var sampleRate = 16_000.0
        var bits: UInt16 = 16
        var channels: UInt16 = 1
        var payload: Data?
        while offset + 8 <= data.count {
            let id = String(data: data.subdata(in: offset..<(offset + 4)), encoding: .ascii) ?? ""
            let size = Int(u32LE(data, offset + 4))
            let start = offset + 8
            guard start <= data.count else { return nil }
            let end = min(data.count, start + size)
            if id == "fmt ", end - start >= 16 {
                channels = u16LE(data, start + 2)
                sampleRate = Double(u32LE(data, start + 4))
                bits = u16LE(data, start + 14)
            } else if id == "data" {
                payload = data.subdata(in: start..<end)
                break
            }
            offset = start + size + (size % 2)
        }
        guard let payload, bits == 16, channels == 1, sampleRate > 0 else { return nil }
        return WavPCM(samples: payload, sampleRate: sampleRate)
    }

    private static func u16LE(_ data: Data, _ offset: Int) -> UInt16 {
        UInt16(data[offset]) | UInt16(data[offset + 1]) << 8
    }

    private static func u32LE(_ data: Data, _ offset: Int) -> UInt32 {
        UInt32(data[offset])
            | UInt32(data[offset + 1]) << 8
            | UInt32(data[offset + 2]) << 16
            | UInt32(data[offset + 3]) << 24
    }

    private func ensureStarted(sampleRate: Double) -> AVAudioFormat? {
        if format == nil {
            format = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: false
            )
        }
        guard let format else { return nil }
        if !attached {
            engine.attach(node)
            engine.connect(node, to: engine.mainMixerNode, format: format)
            attached = true
        }
        if !engine.isRunning {
            do {
                engine.prepare()
                try engine.start()
            } catch {
                node.stop()
                node.reset()
                startTask?.cancel()
                startTask = nil
                startDeadline = .distantPast
                generation += 1
                pendingBuffers = 0
                pendingFrames = 0
                captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
                engine.disconnectNodeOutput(node)
                engine.detach(node)
                attached = false
                self.format = nil
                return nil
            }
        }
        return format
    }
}
#endif
