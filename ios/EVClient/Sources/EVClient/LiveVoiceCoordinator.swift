import Combine
import Foundation
#if os(iOS) || os(macOS)
import AVFoundation
#endif

/// Full-duplex live conversation for iOS (and shared-source inspection on macOS).
///
/// Mac EV.app keeps using `LiveConversation` — this coordinator does not replace
/// that audio graph. iOS opens `POST /v1/voice/live/open` and streams PCM on
/// `WS /v1/voice/live` with the existing `LiveVoiceConnection` / microphone.
@MainActor
public final class LiveVoiceCoordinator: ObservableObject {
    @Published public private(set) var isActive = false
    @Published public private(set) var isRunning = false
    @Published public private(set) var isMuted = false
    @Published public private(set) var isPaused = false
    @Published public var hudCard: HUDCard?
    @Published public var conversationId: String?
    @Published public var sessionId: String?
    @Published public var lastError: String?
    @Published public var confirmingHud = false
    @Published public var transcript = ""
    @Published public private(set) var cameraState: CameraStateSnapshot = .unknown
    @Published public private(set) var cameraRequestInFlight = false
    @Published public private(set) var capabilityManifest = CapabilityManifest()
    @Published public private(set) var deviceMesh = DeviceMeshSnapshot()
    @Published public private(set) var activeLiveProvider: String?

    private var client: EVAPIClient?
    private var deviceId: String = ""
    private var connection: LiveVoiceConnection?
#if os(iOS) || os(macOS)
    private let microphone = LiveVoiceMicrophone()
    private let player = LivePCMPlayer()
#endif
    private var loopTask: Task<Void, Never>?
    private var stayMuted = false
    private var mutedAt: Date?
    private var lastPartialRenderAt = Date.distantPast
    private let partialRenderInterval: TimeInterval = 0.12

    public init() {
#if os(iOS) || os(macOS)
        player.onPlayingChange = { [weak self] playing in
            Task { @MainActor in
                self?.connection?.sendPlayback(active: playing)
            }
        }
        player.onError = { [weak self] detail in
            Task { @MainActor in
                self?.lastError = "Audio playback failed: \(detail). I’ll retry on the next chunk."
            }
        }
#endif
    }

    public func start(client: EVAPIClient, deviceId: String) {
        self.client = client
        self.deviceId = deviceId
        guard loopTask == nil else { return }
        stayMuted = false
        isMuted = false
        isRunning = true
        Task { [weak self] in
            await self?.refreshDeviceMesh()
        }
        Task { [weak self] in
            guard let self else { return }
            do {
                let health = try await client.health()
                self.capabilityManifest = health.capabilityManifest
                self.activeLiveProvider = health.providers?["live"]?.stringValue
            } catch {
                // Live connection errors remain visible through the normal
                // reconnect path; capability discovery is best effort.
            }
        }
        loopTask = Task { [weak self] in
            await self?.runLoop()
        }
    }

    public func stop() {
        loopTask?.cancel()
        loopTask = nil
        tearDownChannel()
        isActive = false
        isRunning = false
        isPaused = false
        cameraRequestInFlight = false
        cameraState = .unknown
    }

    public func toggleMute() {
        if isMuted {
            let stale = mutedAt.map { Date().timeIntervalSince($0) >= 20 } ?? false
            mutedAt = nil
            isMuted = false
            stayMuted = false
            lastError = nil
            if stale || connection == nil || !isActive {
                tearDownChannel()
                isActive = false
                if loopTask == nil {
                    guard let client else { return }
                    start(client: client, deviceId: deviceId)
                }
                return
            }
            connection?.sendControl("resume")
            return
        }
        isMuted = true
        stayMuted = true
        mutedAt = Date()
        connection?.sendControl("mute")
#if os(iOS) || os(macOS)
        microphone.stop()
        player.stop()
#endif
    }

    public func pause() {
        isPaused = true
        connection?.sendControl("pause")
    }

    public func resumeListening() {
        isPaused = false
        isMuted = false
        connection?.sendControl("resume")
    }

    public func bargeIn() {
#if os(iOS) || os(macOS)
        player.stop()
#endif
        connection?.sendControl("barge_in")
    }

    /// Request camera use on the reachable mobile camera node. The state stays
    /// unchanged until Agent 2's provider reports the result.
    public func toggleCamera() {
        let next: CameraState = cameraState.isTruthfullyActive ? .off : .active
        requestCamera(next)
    }

    public func requestCamera(_ state: CameraState) {
        if state == .active {
#if os(iOS)
            let permission = AVCaptureDevice.authorizationStatus(for: .video)
            if permission == .denied || permission == .restricted {
                cameraState = CameraStateSnapshot(
                    state: .denied,
                    visible: false,
                    permissionState: "denied",
                    lastError: "Camera permission denied. Open iOS Settings → EV → Camera."
                )
                lastError = cameraState.lastError
                return
            }
#endif
        }
        guard let connection else {
            lastError = "Camera control is unavailable until the live session connects."
            return
        }
        let target = deviceMesh.preferredCameraNode(preferMac: false)
        cameraRequestInFlight = true
        connection.sendCamera(state, deviceId: target?.id)
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard !Task.isCancelled else { return }
            await MainActor.run {
                self?.cameraRequestInFlight = false
            }
        }
    }

    public func refreshDeviceMesh() async {
        guard let client else { return }
        do {
            let sync = try await client.runtimeSync()
            let nodes = sync.devices.map { device in
                DeviceMeshNode(
                    id: device.deviceId,
                    name: device.name,
                    presence: DevicePresence(rawValue: device.presence),
                    batteryPercent: device.batteryPercent,
                    lastSeenAt: device.lastSeenAt,
                    lastHeartbeatAt: device.lastHeartbeatAt
                )
            }
            deviceMesh = DeviceMeshSnapshot(generatedAt: sync.generatedAt, nodes: nodes)
        } catch {
            // Preserve the last confirmed mesh on a transient network error.
        }
    }

    public func confirmHold() {
        guard !confirmingHud, let card = hudCard, card.isApprovalHold,
              let name = card.holdToolName, !name.isEmpty else { return }
        confirmingHud = true
        Task { [weak self] in
            guard let self else { return }
            defer { self.confirmingHud = false }
            if EVLifeBiometric.isAvailable {
                let ok = await EVLifeBiometric.confirmLifeAction(
                    reason: "Confirm \(name.replacingOccurrences(of: "_", with: " "))"
                )
                guard ok else {
                    self.lastError = "Confirmation cancelled"
                    return
                }
            }
            do {
                if let actionId = card.holdActionId, !actionId.isEmpty {
                    let proof = try? await self.client?.issueReverification(
                        purpose: "runtime.action",
                        voiceSessionId: self.sessionId
                    )
                    let response = try await self.client?.approveAction(
                        id: actionId,
                        reverifyToken: proof?.token
                    )
                    if response?.status == "executed" || response?.status == "approved" {
                        self.lastError = nil
                        self.hudCard = nil
                    } else {
                        self.lastError = response?.error ?? "Confirmation failed"
                    }
                    return
                }
                var arguments = card.holdArguments
                arguments["confirm"] = true
                let response = try await self.client?.dispatchTool(
                    name: name,
                    arguments: arguments,
                    confirm: true,
                    allowSensitive: true
                )
                if response?.ok == true {
                    self.lastError = nil
                    self.hudCard = nil
                } else {
                    self.lastError = response?.error ?? "Confirmation failed"
                }
            } catch {
                self.lastError = Self.renderLiveError(error)
            }
        }
    }

    private func runLoop() async {
        while !Task.isCancelled, isRunning {
            if stayMuted {
                try? await Task.sleep(nanoseconds: 400_000_000)
                continue
            }
            do {
                try await connectOnce()
            } catch is CancellationError {
                return
            } catch {
                if Task.isCancelled { return }
                lastError = Self.renderLiveError(error)
                isActive = false
                tearDownChannel()
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    private func connectOnce() async throws {
        guard let client else { return }
        let opened = try await client.openLiveVoice(deviceId: deviceId)
        lastPartialRenderAt = .distantPast
        sessionId = opened.sessionId
        if let id = opened.conversationId, !id.isEmpty {
            conversationId = id
            UserDefaults.standard.set(id, forKey: "EV_LIVE_CONVERSATION_ID")
        }
        let connection = LiveVoiceConnection(baseURL: client.baseURL, token: client.token)
        self.connection = connection
        let stream = try await connection.connect(sessionId: opened.sessionId)
        isActive = true
        isMuted = false
        isPaused = false
        lastError = nil
        do {
            var microphoneStarted = false
            for try await event in stream {
                if Task.isCancelled { break }
                if !microphoneStarted, event.type == "ready" {
                    microphoneStarted = startMicrophone(on: connection)
                }
                handle(event)
                if event.fatal { break }
            }
        } catch is CancellationError {
            tearDownChannel()
            throw CancellationError()
        }
        tearDownChannel()
        isActive = false
        cameraState = .unknown
        if !stayMuted, !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 400_000_000)
        }
    }

    @discardableResult
    private func startMicrophone(on connection: LiveVoiceConnection) -> Bool {
#if os(iOS) || os(macOS)
        microphone.stop()
        do {
            let player = self.player
            try microphone.start(enqueue: { [weak connection, weak player] data in
                guard player?.shouldMuteCapture != true else { return }
                connection?.enqueuePCM(data)
            })
            isMuted = false
            return true
        } catch {
            lastError = "Microphone capture failed: \(error.localizedDescription)"
            isMuted = true
            return false
        }
#endif
        return true
    }

    private func handle(_ event: LiveVoiceEvent) {
        if let camera = event.cameraState {
            cameraState = camera
            cameraRequestInFlight = false
            if let message = camera.lastError, !message.isEmpty {
                lastError = message
            }
        }
        if let manifest = event.capabilityManifest {
            capabilityManifest = manifest
        }
        if event.deviceMeshReported {
            deviceMesh = DeviceMeshSnapshot(nodes: event.deviceMesh)
        }
        switch event.type {
        case "ready":
            if let id = event.conversationId, !id.isEmpty {
                conversationId = id
            }
            if let id = event.sessionId, !id.isEmpty {
                sessionId = id
            }
            if let provider = event.config["brain"]?.stringValue {
                activeLiveProvider = provider
            }
            if let paused = event.config["paused"]?.boolValue {
                isPaused = paused
            }
            if let muted = event.config["muted"]?.boolValue {
                isMuted = muted
            }
        case "final_transcript":
            if let text = event.text, !text.isEmpty {
                lastPartialRenderAt = .distantPast
                transcript = text
            }
        case "partial":
            if let text = event.text, !text.isEmpty {
                let now = Date()
                guard now.timeIntervalSince(lastPartialRenderAt) >= partialRenderInterval
                    || transcript.isEmpty
                else { break }
                lastPartialRenderAt = now
                transcript = text
            }
        case "reply":
            if let text = event.text, !text.isEmpty {
                transcript = text
            }
#if os(iOS) || os(macOS)
            if !player.shouldMuteCapture {
                connection?.sendPlayback(active: false)
            }
#endif
        case "hud":
            if let card = event.hud {
                hudCard = card
            }
        case "tts_chunk":
            if let b64 = event.audioB64, let data = Data(base64Encoded: b64) {
#if os(iOS) || os(macOS)
                player.enqueue(
                    data,
                    sampleRate: Double(event.sampleRate ?? 16_000),
                    contentType: event.contentType
                )
#endif
            }
        case "barge_in":
#if os(iOS) || os(macOS)
            player.stop()
#endif
        case "state":
            if let paused = event.state["paused"] {
                isPaused = paused == "true"
            }
            if let muted = event.state["muted"] {
                isMuted = muted == "true" || muted == "1"
            }
        case "error":
            if event.code == "realtime_disconnect" {
                player.stop()
                lastError = "Realtime voice disconnected. I’ll keep this session and reconnect."
            } else if event.code == "realtime_connect" {
                lastError = nil
                player.stop()
#if os(iOS) || os(macOS)
                do {
                    try microphone.recover()
                    isMuted = false
                } catch {
                    lastError = "Microphone recovery failed: \(error.localizedDescription)"
                    isMuted = true
                }
#endif
            } else if event.code?.lowercased().contains("camera") == true {
                cameraRequestInFlight = false
                cameraState = CameraStateSnapshot(
                    state: .error,
                    visible: false,
                    deviceId: deviceMesh.preferredCameraNode(preferMac: false)?.id,
                    lastError: event.text ?? event.code
                )
                lastError = cameraState.lastError
            } else if let text = event.text, !text.isEmpty {
                lastError = text
            }
        case "camera_request":
            let action = (event.action ?? "").lowercased()
            if ["capture", "look", "once"].contains(action) {
                Task { await self.fulfillLookCapture(deviceId: event.deviceId) }
            }
        default:
            break
        }
    }

    private func fulfillLookCapture(deviceId: String?) async {
        guard let client, let connection else {
            lastError = "Camera look is unavailable until the live session connects."
            return
        }
#if os(iOS) || os(macOS)
        do {
            let jpeg = try await CameraFrameCapture.captureJPEG()
            let uploaded = try await client.attach(
                filename: "look.jpg",
                contentType: "image/jpeg",
                data: jpeg,
                source: "ios",
                eventType: "camera.look",
                privacyLevel: "normal",
                deviceID: deviceId ?? self.deviceId
            )
            connection.sendLookFrame(
                attachmentId: uploaded.attachment.id,
                deviceId: deviceId ?? self.deviceId
            )
        } catch {
            lastError = error.localizedDescription
            connection.sendLookFrame(
                attachmentId: "",
                deviceId: deviceId ?? self.deviceId
            )
        }
#endif
    }

    private static func renderLiveError(_ error: Error) -> String {
        if let apiError = error as? EVAPIError {
            return apiError.localizedDescription
        }
        return "Live voice failed: \(error.localizedDescription)"
    }

    private func tearDownChannel() {
        connection?.close()
        connection = nil
#if os(iOS) || os(macOS)
        microphone.stop()
        player.stop()
#endif
    }
}
