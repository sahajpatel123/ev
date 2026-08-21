import AppKit
import AVFoundation
import EVClient
import EVRuntime
import Foundation

/// Full-duplex conversation that starts when EV.app opens.
///
/// Opening the app is the door: this opens `POST /v1/voice/live/open`,
/// streams 16 kHz PCM into `WS /v1/voice/live`, plays streamed TTS, and
/// stops playback on barge-in. No wake word is required.
@MainActor
final class LiveConversation {
    private(set) var isActive = false
    private(set) var isMuted = false
    /// True while the reconnect loop is scheduled (including between sockets).
    var isRunning: Bool { loopTask != nil }

    private weak var model: AppModel?
    private var connection: LiveVoiceConnection?
    private let microphone = LiveVoiceMicrophone()
    private var loopTask: Task<Void, Never>?
    private var assistantID: String?
    private var stayMuted = false
    private var mutedAt: Date?
    private var cameraRequestTask: Task<Void, Never>?
    private var computerRequestTask: Task<Void, Never>?
    private var computerStateTask: Task<Void, Never>?
    private var lastComputerFingerprint = ""
    private var cameraLifecycleObservers: [NSObjectProtocol] = []
    private var audioGraphObservers: [NSObjectProtocol] = []
    private var lastPartialRenderAt = Date.distantPast
    private let partialRenderInterval: TimeInterval = 0.12

    deinit {
        for observer in cameraLifecycleObservers {
            NotificationCenter.default.removeObserver(observer)
        }
        for observer in audioGraphObservers {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    func attach(_ model: AppModel) {
        self.model = model
        installCameraLifecycleObservers()
        installAudioGraphObservers()
        model.player.onPlayingChange = { [weak self] playing in
            Task { @MainActor in
                guard let self else { return }
                self.connection?.sendPlayback(active: playing)
                guard let model = self.model else { return }
                if playing {
                    model.status = .speaking
                } else if model.status == .speaking {
                    model.status = .listening
                }
            }
        }
    }

    func start() {
        guard loopTask == nil else { return }
        stayMuted = false
        isMuted = false
        EarsProcess.stopAndWait()
        loopTask = Task { [weak self] in
            await self?.runLoop()
        }
    }

    func stop() {
        stopCameraForSleepOrShutdown()
        cameraRequestTask?.cancel()
        cameraRequestTask = nil
        computerRequestTask?.cancel()
        computerRequestTask = nil
        computerStateTask?.cancel()
        computerStateTask = nil
        loopTask?.cancel()
        loopTask = nil
        tearDownChannel()
        isActive = false
        model?.isLiveActive = false
        model?.isLivePaused = false
        model?.cameraRequestInFlight = false
        model?.cameraState = .unknown
        model?.noteLiveStopped()
    }

    /// Close only the current channel after AppConfig changes. The live loop
    /// remains the owner of the microphone and reconnects through the new
    /// ``EVAPIClient`` on its normal path.
    func configurationDidChange() {
        guard isRunning else { return }
        tearDownChannel()
        isActive = false
        model?.isLiveActive = false
        model?.noteLiveDisconnected(
            reason: "Live configuration changed; reconnecting through AppConfig.",
            willReconnect: true
        )
    }

    private func installCameraLifecycleObservers() {
        guard cameraLifecycleObservers.isEmpty else { return }
        let center = NotificationCenter.default
        for name in [
            NSWorkspace.screensDidSleepNotification,
            NSWorkspace.sessionDidResignActiveNotification,
        ] {
            cameraLifecycleObservers.append(
                center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                    self?.stopCameraForSleepOrShutdown()
                }
            )
        }
    }

    private func installAudioGraphObservers() {
        guard audioGraphObservers.isEmpty else { return }
        audioGraphObservers.append(
            NotificationCenter.default.addObserver(
                forName: .AVAudioEngineConfigurationChange,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                self?.recoverMicrophoneAfterGraphChange()
            }
        )
    }

    private func recoverMicrophoneAfterGraphChange() {
        guard isActive, !isMuted, let connection else { return }
        do {
            try microphone.recover()
        } catch {
            _ = startMicrophone(on: connection)
        }
    }

    private func stopCameraForSleepOrShutdown() {
        guard let model, model.cameraState.isTruthfullyActive else { return }
        connection?.sendCamera(.off, deviceId: model.cameraState.deviceId)
        model.cameraRequestInFlight = true
    }

    func toggleMute() {
        if isMuted {
            let stale = mutedAt.map { Date().timeIntervalSince($0) >= 20 } ?? false
            mutedAt = nil
            isMuted = false
            stayMuted = false
            model?.isLiveMuted = false
            model?.lastError = nil
            if stale || connection == nil || !isActive {
                // A long mute can outlive the WebSocket or its upstream
                // realtime session. Restart the normal connection loop instead
                // of requiring the owner to quit and relaunch EV.
                tearDownChannel()
                isActive = false
                model?.isLiveActive = false
                model?.noteLiveDisconnected(
                    reason: "Mute exceeded the live-session recovery window; reconnecting.",
                    willReconnect: true
                )
                if loopTask == nil {
                    start()
                }
                return
            }
            if let connection, isActive {
                if startMicrophone(on: connection) {
                    model?.noteLiveConnected()
                    connection.sendControl("resume")
                }
            }
            return
        }
        isMuted = true
        stayMuted = true
        mutedAt = Date()
        model?.isLiveMuted = true
        model?.player.stop()
        microphone.stop()
        VoiceLevelMeter.shared.resetInput()
        // Detaching a node from a running AVAudioEngine can abort macOS.
        model?.player.bind(to: nil)
        connection?.sendControl("mute")
        model?.status = .listening
        model?.noteLiveMuted()
    }

    func toggleCamera() {
        guard let model else { return }
        guard let connection else {
            model.lastError = "Camera control is unavailable until the live session connects."
            return
        }
        let next: CameraState = model.cameraState.isTruthfullyActive ? .off : .active
        if next == .active, let local = model.localCameraPermissionState(), local == .denied {
            model.cameraState = CameraStateSnapshot(
                state: .denied,
                visible: false,
                permissionState: "denied",
                lastError: "Camera permission denied. Open EV → Permissions to grant access."
            )
            model.lastError = model.cameraState.lastError
            return
        }
        let target = model.deviceMesh.preferredCameraNode(preferMac: true)
        model.cameraRequestInFlight = true
        connection.sendCamera(next, deviceId: target?.id)
        cameraRequestTask?.cancel()
        cameraRequestTask = Task { [weak model] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard !Task.isCancelled else { return }
            await MainActor.run {
                // A request timeout is not an activation report. Leave the
                // last provider state intact and only release the spinner.
                model?.cameraRequestInFlight = false
            }
        }
    }

    private func fulfillLookCapture(deviceId: String?, requestId: String?) async {
        guard let model else { return }
        guard let connection else {
            model.lastError = "Camera look is unavailable until the live session connects."
            return
        }
        let permission = CameraManager.shared.permissionState()
        do {
            let frame = try await CameraManager.shared.captureFrame()
            connection.sendLookFrame(
                requestId: requestId,
                jpeg: frame.jpeg,
                width: frame.width,
                height: frame.height,
                error: nil,
                permission: frame.permission,
                deviceId: deviceId ?? model.cameraState.deviceId,
                sequence: 0,
                last: true,
                cameraName: frame.cameraName
            )
        } catch {
            let code: String
            if let capture = error as? CameraManager.CaptureError {
                code = capture.code
            } else {
                code = "capture_failed"
            }
            model.lastError = error.localizedDescription
            connection.sendLookFrame(
                requestId: requestId,
                jpeg: nil,
                width: nil,
                height: nil,
                error: code,
                permission: permission,
                deviceId: deviceId ?? model.cameraState.deviceId,
                last: true
            )
        }
    }

    private func fulfillObserve(
        deviceId: String?,
        requestId: String?,
        durationMs: Int?,
        intervalMs: Int?,
        maxFrames: Int?
    ) {
        guard let connection else { return }
        let duration = TimeInterval(durationMs ?? 4000) / 1000
        let interval = TimeInterval(intervalMs ?? 1500) / 1000
        let frames = maxFrames ?? 5
        let resolvedDeviceId = deviceId ?? model?.cameraState.deviceId
        CameraManager.shared.observe(
            duration: duration,
            interval: interval,
            maxFrames: frames
        ) { [weak connection] result, index, last in
            guard let connection else { return }
            switch result {
            case .success(let frame):
                connection.sendLookFrame(
                    requestId: requestId,
                    jpeg: frame.jpeg,
                    width: frame.width,
                    height: frame.height,
                    error: nil,
                    permission: frame.permission,
                    deviceId: resolvedDeviceId,
                    sequence: index,
                    last: last,
                    cameraName: frame.cameraName
                )
            case .failure(let error):
                let code = (error as? CameraManager.CaptureError)?.code ?? "capture_failed"
                connection.sendLookFrame(
                    requestId: requestId,
                    jpeg: nil,
                    width: nil,
                    height: nil,
                    error: code,
                    permission: CameraManager.shared.permissionState(),
                    deviceId: resolvedDeviceId,
                    sequence: index,
                    last: true
                )
            }
        }
    }

    private func fulfillComputer(_ event: LiveVoiceEvent) {
        let command = event.command ?? event.action ?? ""
        let requestId = event.requestId ?? "computer-\(Int(Date().timeIntervalSince1970 * 1000))"
        let arguments = event.argumentObject
        let deviceId = event.deviceId ?? model?.cameraState.deviceId
        computerRequestTask = Task.detached(priority: .userInitiated) { [weak self] in
            if command == "cancel" {
                MacControlService.shared.cancel(requestId: requestId)
            }
            let result = MacControlService.shared.handle(
                command: command,
                arguments: arguments,
                requestId: requestId
            )
            let jpeg = result["jpeg"] as? Data
            var payload = result
            payload.removeValue(forKey: "jpeg")
            let snapshot = payload
            let device = deviceId
            let cmd = command
            let rid = requestId
            let conversation = self
            await MainActor.run {
                if let error = snapshot["error"] as? String, error == "accessibility_denied" {
                    conversation?.model?.needsComputerAccessibility = true
                    conversation?.model?.lastError =
                        "I can open apps, but macOS hasn't given EV Accessibility access yet. Open Permissions and enable it."
                }
                conversation?.connection?.sendComputerResult(
                    requestId: rid,
                    command: cmd,
                    result: snapshot,
                    jpeg: jpeg,
                    deviceId: device
                )
            }
        }
    }

    private func startComputerStateWatch(deviceId: String?) {
        computerStateTask?.cancel()
        lastComputerFingerprint = ""
        publishComputerStateIfChanged(deviceId: deviceId)
        computerStateTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run {
                    self?.publishComputerStateIfChanged(deviceId: deviceId)
                }
            }
        }
    }

    private func publishComputerStateIfChanged(deviceId: String?) {
        let snap = MacControlService.shared.permissionSnapshot()
        let fp = [
            String(describing: snap["accessibility_permission"] ?? ""),
            String(describing: snap["generic_ui_control_ready"] ?? false),
            String(describing: snap["screen_capture_permission"] ?? ""),
        ].joined(separator: "|")
        guard fp != lastComputerFingerprint else { return }
        lastComputerFingerprint = fp
        connection?.sendComputerState(snap, deviceId: deviceId)
        if snap["generic_ui_control_ready"] as? Bool == true {
            model?.needsComputerAccessibility = false
            if model?.lastError?.localizedCaseInsensitiveContains("accessibility") == true {
                model?.lastError = nil
            }
        }
    }

    private func runLoop() async {
        defer {
            loopTask = nil
            isActive = false
            model?.isLiveActive = false
        }
        guard let model else { return }
        let granted = await MicrophoneAuthorization.requestAccess()
        if !granted {
            model.noteMicrophoneDenied()
            return
        }
        if model.config.usesPlaceholderKey {
            model.lastError = "API key is still the placeholder. Live listen cannot start."
            if model.status == .offline {
                await model.refreshHealth()
            }
            return
        }
        while !Task.isCancelled {
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
                let rendered = modelFormatted(error)
                model.noteLiveDisconnected(reason: rendered, willReconnect: true)
                model.lastError = rendered
                model.status = .offline
                isActive = false
                model.isLiveActive = false
                tearDownChannel()
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    private func connectOnce() async throws {
        guard let model else { return }
        model.resetLiveDiagnostics()
        lastPartialRenderAt = .distantPast
        let registry = UserDefaults.standard.string(forKey: "EV_REGISTRY_DEVICE_ID")
        let deviceId: String
        if let registry, UUID(uuidString: registry) != nil {
            deviceId = registry
        } else {
            deviceId = model.config.deviceID
        }
        model.noteLiveConnectionAttempt(deviceID: deviceId)
        let opened = try await model.client.openLiveVoice(deviceId: deviceId)
        model.noteLiveSessionOpened(sessionID: opened.sessionId, deviceID: deviceId)
        let connection = LiveVoiceConnection(
            baseURL: model.client.baseURL,
            token: model.client.token
        )
        self.connection = connection
        let stream = try await connection.connect(sessionId: opened.sessionId)
        isActive = true
        model.noteLiveConnected()
        model.isLiveActive = true
        model.isLiveMuted = false
        model.isLivePaused = false
        model.status = .listening
        model.lastError = nil

        do {
            var microphoneStarted = false
            for try await event in stream {
                if Task.isCancelled { break }
                if !microphoneStarted, event.type == "ready" {
                    microphoneStarted = startMicrophone(on: connection)
                    connection.sendCameraReadiness(
                        permission: CameraManager.shared.permissionState(),
                        deviceId: deviceId,
                        cameraName: nil
                    )
                    connection.sendComputerState(
                        MacControlService.shared.permissionSnapshot(),
                        deviceId: deviceId
                    )
                    startComputerStateWatch(deviceId: deviceId)
                }
                await handle(event)
                if event.fatal {
                    // Fatal is a channel close, never a process quit.
                    if event.code == "listening_stopped" {
                        stayMuted = true
                        isMuted = true
                        model.isLiveMuted = true
                        model.player.stop()
                        microphone.stop()
                        VoiceLevelMeter.shared.resetInput()
                        model.player.bind(to: nil)
                        model.noteLiveMuted()
                    }
                    break
                }
            }
        } catch is CancellationError {
            tearDownChannel()
            throw CancellationError()
        }
        tearDownChannel()
        isActive = false
        model.isLiveActive = false
        model.cameraState = .unknown
        if Task.isCancelled {
            // Explicit shutdown owns the final diagnostics state and the
            // teardown ordering; do not overwrite ``stopped`` from here.
        } else if stayMuted {
            model.noteLiveMuted()
        } else {
            model.noteLiveDisconnected(
                reason: "Live channel closed.",
                willReconnect: true
            )
        }
        if !stayMuted, !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 400_000_000)
        }
    }

    @discardableResult
    private func startMicrophone(on connection: LiveVoiceConnection) -> Bool {
        microphone.stop()
        // Stop capture before changing the playback graph (-10867 safety).
        model?.player.bind(to: nil)
        guard AudioInputLease.acquire(.live) else {
            model?.noteMicrophoneCaptureFailed("already in use")
            return false
        }
        do {
            let player = model?.player
            try microphone.start(enqueue: { [weak connection, weak player] data in
                VoiceLevelMeter.shared.ingestInputPCM16(data)
                if player?.shouldMuteCapture == true { return }
                connection?.enqueuePCM(data)
            })
            isMuted = false
            model?.isLiveMuted = false
            return true
        } catch {
            // Keep the live lease so Talk cannot start a second engine.
            let ns = error as NSError
            if ns.code == -10867 {
                model?.noteMicrophoneCaptureFailed("audio device was busy — unmute again")
            } else {
                model?.noteMicrophoneCaptureFailed(error.localizedDescription)
            }
            return false
        }
    }

    private func handle(_ event: LiveVoiceEvent) async {
        guard let model else { return }
        applyLiveDiagnostics(from: event)
        if let camera = event.cameraState {
            model.cameraState = camera
            model.cameraRequestInFlight = false
            cameraRequestTask?.cancel()
            cameraRequestTask = nil
            if let message = camera.lastError, !message.isEmpty {
                model.lastError = message
            }
        }
        if let manifest = event.capabilityManifest {
            model.capabilityManifest = manifest
        }
        if event.deviceMeshReported {
            model.deviceMesh = DeviceMeshSnapshot(nodes: event.deviceMesh)
        }
        switch event.type {
        case "ready":
            model.lastError = nil
            model.status = .listening
            model.isLivePaused = false
            if let conversationId = event.conversationId, !conversationId.isEmpty {
                model.conversationId = conversationId
            }
            if let sessionId = event.sessionId, !sessionId.isEmpty {
                model.sessionId = sessionId
            }
            if let provider = event.config["brain"]?.stringValue {
                model.activeLiveProvider = provider
            }
        case "state":
            apply(phase: event.state["phase"])
            if let paused = event.state["paused"] {
                model.isLivePaused = paused == "true" || paused == "1"
            }
        case "partial":
            if let text = event.text, !text.isEmpty {
                let now = Date()
                guard now.timeIntervalSince(lastPartialRenderAt) >= partialRenderInterval
                    || model.transcript.isEmpty
                else { break }
                lastPartialRenderAt = now
                model.transcript = text
            }
        case "final_transcript":
            if let text = event.text, !text.isEmpty {
                lastPartialRenderAt = .distantPast
                model.lastError = nil
                model.transcript = text
                model.messages.append(
                    AppModel.ChatMessage(id: UUID().uuidString, role: "user", text: text, streaming: false)
                )
                let id = UUID().uuidString
                assistantID = id
                model.messages.append(
                    AppModel.ChatMessage(id: id, role: "assistant", text: "", streaming: true)
                )
                model.status = .thinking
            }
        case "backchannel":
            await playAudio(event)
        case "tts_chunk":
            model.lastError = nil
            if let text = event.text, !text.isEmpty, let id = assistantID,
               let index = model.messages.firstIndex(where: { $0.id == id }),
               model.messages[index].text.isEmpty {
                model.messages[index].text = text
            }
            await playAudio(event)
        case "reply":
            model.lastError = nil
            if let text = event.text {
                if let id = assistantID, let index = model.messages.firstIndex(where: { $0.id == id }) {
                    model.messages[index].text = text
                    model.messages[index].streaming = false
                } else if !text.isEmpty {
                    model.messages.append(
                        AppModel.ChatMessage(id: UUID().uuidString, role: "assistant", text: text, streaming: false)
                    )
                }
            }
            assistantID = nil
            model.player.noteAssistantAudioComplete()
            if !model.player.isPlaying {
                model.status = .listening
                connection?.sendPlayback(active: false)
            }
        case "barge_in":
            model.player.stop()
            connection?.sendPlayback(active: false)
            model.status = .listening
        case "hud":
            if let card = event.hud {
                model.hudCard = card
                applyToolCard(card)
            }
        case "error":
            let message = event.text ?? event.code ?? ""
            if Self.isBenignRealtimeError(message) {
                break
            }
            if event.code == "realtime_disconnect" {
                model.player.stop()
                model.noteLiveDisconnected(
                    reason: "Realtime provider disconnected; backend is retrying upstream.",
                    willReconnect: true
                )
                model.lastError = "Realtime voice disconnected. I’ll keep this session and reconnect."
                break
            }
            if event.code == "realtime_connect" {
                model.lastError = nil
                model.player.stop()
                if let connection {
                    _ = startMicrophone(on: connection)
                }
                break
            }
            if event.code == "realtime_tools_rejected" {
                let acknowledged = Self.acknowledgedToolNames(from: message) ?? []
                model.noteProviderToolMismatch(acknowledgedTools: acknowledged, message: message)
                model.noteLiveCapabilityError(message)
            } else if event.code == "live_capabilities_unavailable" {
                model.applyLiveDiagnostics(capabilityError: message)
                model.noteLiveCapabilityError(message)
            }
            if event.code?.lowercased().contains("camera") == true {
                model.cameraRequestInFlight = false
                model.cameraState = CameraStateSnapshot(
                    state: .error,
                    visible: false,
                    deviceId: model.deviceMesh.preferredCameraNode()?.id,
                    lastError: message.isEmpty ? "Camera failed." : message
                )
            }
            if !message.isEmpty {
                model.lastError = message
            }
            if model.status == .speaking, !model.player.isPlaying {
                model.status = .listening
            }
        case "camera_request":
            let action = (event.action ?? "").lowercased()
            if action == "observe_stop" {
                CameraManager.shared.cancelObserve()
                break
            }
            if action == "observe" {
                fulfillObserve(
                    deviceId: event.deviceId,
                    requestId: event.requestId,
                    durationMs: event.durationMs,
                    intervalMs: event.intervalMs,
                    maxFrames: event.maxFrames
                )
                break
            }
            if ["capture", "look", "once"].contains(action) {
                await fulfillLookCapture(deviceId: event.deviceId, requestId: event.requestId)
            }
        case "computer_request":
            fulfillComputer(event)
        default:
            break
        }
    }

    private func applyLiveDiagnostics(from event: LiveVoiceEvent) {
        guard let model else { return }
        guard event.realtimeDiagnostics != nil
            || !event.config.isEmpty
            || event.ttsDeviceId != nil
            || event.capabilityManifest != nil
        else { return }
        let manifest = event.config["capability_manifest"]?.objectValue
        let realtime = event.realtimeDiagnostics ?? event.config["realtime"]?.objectValue
        let runtimeManifest = manifest?["runtime_manifest"]?.objectValue

        let advertisedValue = firstValue([
            realtime?["advertised_tool_names"],
            realtime?["tool_names"],
            manifest?["realtime_tool_names"],
            manifest?["tool_names"],
            manifest?["realtime_tools"],
            manifest?["live_tool_projection"],
        ])
        let acknowledgedValue = firstValue([
            realtime?["acknowledged_tool_names"],
            realtime?["upstream_tool_names"],
            manifest?["upstream_tool_names"],
        ])
        let provider = firstString([
            realtime?["provider"],
            event.config["brain"],
            manifest?["current_provider"],
        ])
        let liveModel = firstString([
            event.config["model"],
            event.config["realtime_model"],
            realtime?["model"],
            manifest?["model"],
            runtimeManifest?["model"],
            runtimeManifest?["realtime_model"],
            runtimeManifest?["current_model"],
        ])
        let ttsDeviceID = event.ttsDeviceId
            ?? firstString([
                event.config["tts_device_id"],
                manifest?["tts_device_id"],
            ])
        let capabilityError = firstString([
            realtime?["capability_error"],
            manifest?["capability_error"],
            event.config["capability_error"],
        ])
        let providerReady = realtime?["upstream_session_ready"]?.boolValue
        let providerDiagnosticsReported = realtime != nil || acknowledgedValue != nil

        let advertisedTools = advertisedValue.map(Self.toolNames(from:)) ?? []
        let acknowledgedTools = acknowledgedValue.map(Self.toolNames(from:)) ?? []
        model.applyLiveDiagnostics(
            provider: provider,
            model: liveModel,
            ttsDeviceID: ttsDeviceID,
            advertisedTools: advertisedValue.map(Self.toolNames(from:)),
            acknowledgedTools: acknowledgedValue.map(Self.toolNames(from:)),
            toolsReported: advertisedValue != nil,
            providerToolsReported: providerDiagnosticsReported,
            providerSessionReady: providerReady,
            capabilityError: capabilityError
        )
        model.noteLiveRuntime(
            provider: provider,
            model: liveModel,
            advertisedTools: advertisedTools,
            providerAcknowledgedTools: acknowledgedTools,
            providerSessionReady: providerReady ?? model.liveRuntimeDiagnostics.providerSessionReady,
            capabilityErrors: capabilityError.map { [$0] } ?? model.liveRuntimeDiagnostics.capabilityErrors
        )

    }

    private func applyToolCard(_ card: HUDCard) {
        guard let model else { return }
        model.noteLiveToolHUD(card)
        let meta = card.meta ?? [:]
        let name = meta["tool"]?.stringValue ?? ""
        switch card.metaKind {
        case "progress":
            model.noteLiveToolProgress(name: name)
        case "approval_hold":
            let expiry = meta["expires_at"]?.stringValue
                ?? meta["expiry"]?.stringValue
                ?? meta["ttl_seconds"]?.numberValue.map { "\(Int($0)) sec" }
            model.noteLiveConfirmationHold(
                action: name,
                target: meta["target"]?.stringValue,
                method: meta["confirmation_channel"]?.stringValue
                    ?? meta["method"]?.stringValue,
                expiry: expiry
            )
        case "evidence":
            model.noteLiveToolEvidence(
                name: name,
                source: meta["source"]?.stringValue,
                timestamp: meta["timestamp"]?.stringValue ?? card.generatedAt
            )
        case "tool_result":
            model.noteLiveToolResult(
                name: name,
                success: meta["success"]?.boolValue ?? false
            )
        default:
            break
        }
    }

    private func firstValue(_ values: [AnyCodable?]) -> AnyCodable? {
        values.compactMap { $0 }.first
    }

    private func firstString(_ values: [AnyCodable?]) -> String? {
        values.compactMap { $0?.stringValue }.first { !$0.isEmpty }
    }

    private static func toolNames(from value: AnyCodable) -> [String] {
        if let string = value.stringValue {
            return string.isEmpty ? [] : [string]
        }
        guard let values = value.arrayValue else { return [] }
        return values.compactMap { item in
            item.stringValue ?? item.objectValue?["name"]?.stringValue
        }
        .filter { !$0.isEmpty }
    }

    private static func acknowledgedToolNames(from message: String) -> [String]? {
        guard let range = message.range(of: "received [") else { return nil }
        let suffix = message[range.upperBound...]
        guard let end = suffix.firstIndex(of: "]") else { return nil }
        let list = String(suffix[..<end]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !list.isEmpty else { return [] }
        return list
            .split(separator: ",")
            .map {
                String($0).trimmingCharacters(in: .whitespacesAndNewlines)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            }
            .filter { !$0.isEmpty && $0.allSatisfy { $0.isLetter || $0.isNumber || "_-.".contains($0) } }
    }

    private func apply(phase: String?) {
        guard let model, let phase else { return }
        switch phase {
        case "thinking", "reasoning":
            model.status = .thinking
        case "speaking", "speaking_and_listening":
            model.status = model.player.isPlaying ? .speaking : .listening
        case "interrupted":
            model.status = .listening
        default:
            if model.status != .speaking || !model.player.isPlaying {
                model.status = .listening
            }
        }
    }

    private func playAudio(_ event: LiveVoiceEvent) async {
        guard let model else { return }
        if let b64 = event.audioB64, let data = Data(base64Encoded: b64), !data.isEmpty {
            do {
                try model.player.enqueue(
                    data,
                    contentType: event.contentType,
                    sampleRate: event.sampleRate ?? 16_000
                )
            } catch {
                model.lastError = "TTS playback failed: \(error.localizedDescription)"
                model.player.recover()
                if model.status == .speaking {
                    model.status = .listening
                }
            }
            return
        }
        if let ref = event.audioRef, !ref.isEmpty {
            do {
                let data = try await model.client.voiceAudio(ref: ref)
                try model.player.enqueue(data)
            } catch {
                model.lastError = "TTS playback failed: \(error.localizedDescription)"
                model.player.recover()
                if model.status == .speaking {
                    model.status = .listening
                }
            }
        }
    }

    private static func isBenignRealtimeError(_ message: String) -> Bool {
        let blob = message.lowercased()
        return blob.contains("no active response")
            || blob.contains("cancellation failed")
            || blob.contains("already cancelled")
            || blob.contains("already canceled")
            || blob.contains("already has an active response")
            || blob.contains("active response in progress")
    }

    private func tearDownChannel() {
        computerStateTask?.cancel()
        computerStateTask = nil
        model?.player.stop()
        microphone.stop()
        VoiceLevelMeter.shared.resetInput()
        model?.player.bind(to: nil)
        AudioInputLease.release(.live)
        connection?.close()
        connection = nil
    }

    private func modelFormatted(_ error: Error) -> String {
        model?.formattedLiveError(error) ?? error.localizedDescription
    }
}
