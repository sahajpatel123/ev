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
    // SPOKEN INTERRUPTION: CLOSED (2026-08-23). ExplicitInterruptMonitor and
    // all spoken-detection machinery are DEAD/LEGACY — removed from the
    // production composition. Deterministic interruption (Escape / Stop
    // Speaking button) uses stopAssistantSpeech() on the main actor below.
    // OWNER DECISION 2026-08-23 — Listener Presence: CANCELLED (removed from
    // the active product; never re-tune, re-enable, or re-wire). Natural
    // Barge-In: superseded by Interruption V1. EVIE_CALM_VOICE golden path:
    // one speech lane, the backend's authoritative-playback mic gate owns
    // self-echo, and the player reports physical truth via
    // onPlayingChange -> sendPlayback.

    /// STARTUP / OFFLINE LIFECYCLE TRACE (P0 2026-08-23): every milestone
    /// lands in ~/Library/Logs/EV/startup-trace.jsonl via a background
    /// writer, so "mic took seconds" resolves to an exact interval
    /// (ST03→ST14) and every OFFLINE carries its causal reason. Never called
    /// from an audio render context.
    /// Launch origin, set on FIRST trace call (race-free): avoids the
    /// static-let lazy-init ordering hazard that produced wrapped deltas.
    nonisolated(unsafe) private static var launchMono: UInt64 = 0
    /// Process-wide one-shot: the FIRST mic PCM frame of this launch. Touched
    /// only from the audio tap; a benign benign race just logs twice at worst.
    nonisolated(unsafe) private static var micFirstFrameLogged = false
    /// Provider readiness for FORWARDING (not for capture): mic runs locally
    /// from WS-connect; PCM goes upstream only after the provider signals
    /// ready. Reset on teardown and on provider loss (cancelled-audio law).
    nonisolated(unsafe) private var providerReadyForForward = false
    /// Mirrors the actual native capture lifecycle. It must be cleared any
    /// time ``microphone.stop()`` runs; otherwise a reconnect can reopen the
    /// backend/provider channel while incorrectly believing the old tap is
    /// still alive.
    private var microphoneStarted = false
    /// ONE APP VOICE INTERACTION HAS ONE AUTHORITATIVE ACTIVE GENERATION.
    /// Every connectOnce increments this; stale generation callbacks must not
    /// mutate the new generation's state (providerReady, player, etc).
    private var generation = 0
    /// Watchdog for provably broken session: speech accepted but no response.
    private var responseWatchdog: Task<Void, Never>?
    private var playbackResponseID: String?
    private var playbackProviderResponseID: String?
    nonisolated(unsafe) private weak var playbackPlayer: TTSPlayer?
    private static let traceLock = NSLock()
    nonisolated private static func st(_ event: String, _ reason: String = "") {
        let now = DispatchTime.now().uptimeNanoseconds
        lockFreeInitLaunch(now)
        let elapsedMs = Double(now &- launchMono) / 1_000_000
        var payload: [String: Any] = [
            "event": event,
            "elapsed_ms": (elapsedMs * 1000).rounded() / 1000,
            "reason": reason,
            "ts_ms": Int(Date().timeIntervalSince1970 * 1000),
        ]
        guard JSONSerialization.isValidJSONObject(payload),
              let line = try? JSONSerialization.data(withJSONObject: payload)
        else { return }
        DispatchQueue.global(qos: .utility).async {
            traceLock.lock()
            defer { traceLock.unlock() }
            let logs = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first
                ?? URL(fileURLWithPath: NSTemporaryDirectory())
            let dir = logs.appendingPathComponent("Logs/EV", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let url = dir.appendingPathComponent("startup-trace.jsonl")
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: nil)
            }
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                var out = line
                out.append(0x0A)
                try? handle.write(contentsOf: out)
            }
        }
    }

    private static let launchInitLock = NSLock()
    nonisolated private static func lockFreeInitLaunch(_ now: UInt64) {
        launchInitLock.lock()
        if launchMono == 0 { launchMono = now }
        launchInitLock.unlock()
    }

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
        playbackPlayer = model.player
        installCameraLifecycleObservers()
        installAudioGraphObservers()
        model.player.onPlayingChange = { [weak self] playing in
            Task { @MainActor in
                guard let self else { return }
                // The client player owns physical playback truth: this report
                // is the backend mic gate's authority (speaker ownership law).
                self.connection?.sendPlayback(active: playing)
                // INTERRUPTION V1: detection arms only while the speaker is
                // physically playing (feature off -> monitor is nil).
                if playing {
                    self.installEscapeStop()
                } else {
                    self.removeEscapeStop()
                }
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
        fullTeardownCapture()
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
            ) { [weak self] notification in
                self?.recoverMicrophoneAfterGraphChange(notification)
            }
        )
    }

    private func recoverMicrophoneAfterGraphChange(_ notification: Notification) {
        // STRICT ENGINE OWNERSHIP: microphone may react ONLY to its own input engine.
        // Output engine (TTSPlayer) posts the same notification name with
        // object == its output engine. With object:nil at registration, we must
        // filter by identity here. Without this, output graph changes spuriously
        // restart the input graph, creating a cyclic feedback loop.
        guard let changed = notification.object as? AVAudioEngine else { return }
        guard changed === microphone.engine else { return }
        guard isActive, !isMuted, let connection else { return }
        // Never churn the audio graph while assistant speech is playing —
        // a route change during TTS would glitch the very audio the owner
        // is hearing. Defer recovery until playback completes.
        if model?.status == .speaking || model?.player.isPlaying == true {
            return
        }
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

    /// DETERMINISTIC STOP (spoken-interruption fallback, 2026-08-23):
    /// immediately silence assistant audio and cancel the active response.
    /// Uses the same safe main-thread path as the rest of the lifecycle.
    func stopAssistantSpeech() {
        // EXACTLY-ONCE UI CONTRACT: a Stop activation with nothing playing is
        // a no-op (no duplicate barge_in/cancel controls, no state churn).
        guard let model, isActive else { return }
        guard model.status == .speaking || model.player.isPlaying else { return }
        let playedMs = model.player.playbackSnapshot().playedMs
        model.player.stop()
        connection?.sendPlayback(active: false)
        connection?.sendControl(
            "barge_in",
            extra: [
                "reason": "ui_stop",
                "audio_played_ms": playedMs,
                "confidence": 1.0,
            ]
        )
        model.status = .listening
    }

    /// Escape key = immediate stop while Evie speaks (deterministic fallback
    /// after spoken interruption was abandoned). Local monitor only; installed
    /// when speech starts, removed when it ends.
    private var escapeMonitor: Any?

    private func installEscapeStop() {
        guard escapeMonitor == nil else { return }
        escapeMonitor = NSEvent.addLocalMonitorForEvents(
            matching: .keyDown
        ) { [weak self] event in
            if event.keyCode == 53, self?.model?.status == .speaking {
                self?.stopAssistantSpeech()
                return nil // consumed
            }
            return event
        }
    }

    private func removeEscapeStop() {
        if let m = escapeMonitor {
            NSEvent.removeMonitor(m)
            escapeMonitor = nil
        }
    }

    func toggleMute() {
        if isMuted {
            // MUTE LAW (P0 voice reliability): mute/unmute is USER MUTE STATE only.
            // It must not rebuild the provider session or repair the audio engine.
            // Low-level failures are handled automatically by the lifecycle.
            mutedAt = nil
            isMuted = false
            stayMuted = false
            model?.isLiveMuted = false
            model?.lastError = nil
            // If the transport is gone, let the single runLoop reconnect — do
            // not create a second loop here. Just resume capture if possible.
            if let connection, isActive, !microphoneStarted {
                _ = startMicrophone(on: connection)
                if microphoneStarted { connection.sendControl("resume") }
            } else if let connection, isActive {
                connection.sendControl("resume")
            }
            // Stale-session case is handled by runLoop's normal reconnect;
            // no tearDownChannel/start() here.
            return
        }
        // Mute = suppress input only.
        isMuted = true
        // stayMuted remains false — runLoop stays scheduled so unmute is instant.
        // Only explicit user mute; the loop's ST21 hold is for listening_stopped.
        mutedAt = Date()
        model?.isLiveMuted = true
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
                Self.st("ST21_LOOP_MUTED_HOLD", "stayMuted")
                try? await Task.sleep(nanoseconds: 400_000_000)
                continue
            }
            Self.st("ST20_LOOP_ITERATE")
            do {
                try await connectOnce()
            } catch is CancellationError {
                Self.st("ST22_LOOP_CANCELLED", "cancellation")
                return
            } catch {
                if Task.isCancelled { return }
                let rendered = modelFormatted(error)
                Self.st("ST16_UNEXPECTED_DISCONNECT", rendered)
                Self.st("ST17_RECONNECT_BEGIN", rendered)
                model.noteLiveDisconnected(reason: rendered, willReconnect: true)
                model.lastError = rendered
                model.status = .offline
                isActive = false
                model.isLiveActive = false
                tearDownChannel()
                // Single bounded backoff authority (was 1.5s here + 0.4s in
                // connectOnce tail = 1.9s stacked). Now only runLoop backs off.
                try? await Task.sleep(nanoseconds: 900_000_000)
            }
        }
        Self.st("ST22_LOOP_CANCELLED", "while-exit")
    }

    private func connectOnce() async throws {
        guard let model else { return }
        generation += 1
        let myGen = generation
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
        Self.st("ST11_BACKEND_CONNECT_BEGIN")
        // STALL FORENSICS: every phase boundary is traced so a silent hang
        // (e.g. an unawaited socket close racing the open POST) resolves to
        // an exact phase instead of "no ST11 after ST16".
        var phase = "ST11"
        do {
            defer { Self.st("ST19_CONNECT_ONCE_EXIT", phase) }
            let opened = try await model.client.openLiveVoice(deviceId: deviceId)
            phase = "ST12"
            Self.st("ST12_BACKEND_SESSION_OPENED")
            model.noteLiveSessionOpened(sessionID: opened.sessionId, deviceID: deviceId)
            let connection = LiveVoiceConnection(
                baseURL: model.client.baseURL,
                token: model.client.token
            )
            self.connection = connection
            phase = "ST12B_WS_CONNECT"
            // INTERRUPTION V1: construct the detector ONLY when the owner enabled
            // the feature flag. OFF = architecturally absent (no detector, no
            // recognizer, no callbacks, no stop authority). The executor runs on
            // interruptControlQueue: local stop first, then playback report,
            // barge-in control, and preroll forward — never on the audio thread.
            let stream = try await connection.connect(sessionId: opened.sessionId)
            phase = "ST12B_MIC_START"
            // STARTUP DECOUPLING (P0 2026-08-23): LOCAL_MIC_READY must not wait
            // for the remote provider. Capture begins as soon as OUR transport is
            // up; frames are forwarded only after the provider signals ready.
            // Provider outage therefore cannot delay or disable the microphone.
            Self.st("ST12B_WS_CONNECTED_STARTING_MIC_LOCALLY")
            let wasStarted = microphoneStarted
            microphoneStarted = startMicrophone(on: connection)
            if microphoneStarted {
                if wasStarted {
                    Self.st("ST06_MIC_REBOUND_KEEP_ALIVE", "gen \(myGen)")
                } else {
                    Self.st("ST06_MIC_STARTED_LOCAL_FIRST")
                }
            }
            phase = "ST18_OK"
            isActive = true
            Self.st("ST18_RECONNECT_OK")
            model.noteLiveConnected()
            model.isLiveActive = true
            model.isLiveMuted = false
            model.isLivePaused = false
            // UI readiness law: don't claim LISTENING while provider not ready.
            // Show CONNECTING until ST14, then LISTENING.
            model.status = providerReadyForForward ? .listening : .offline
            if !providerReadyForForward {
                model.lastError = nil
                // Will flip to listening at ST14.
            } else {
                model.lastError = nil
            }
            phase = "CONSUME_EVENTS"

        do {
            for try await event in stream {
                if Task.isCancelled { break }
                guard generation == myGen else { break }
                if event.type == "ready", generation == myGen, !providerReadyForForward {
                    providerReadyForForward = true
                    Self.st("ST14_PROVIDER_READY_FORWARDING_OPEN")
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
                guard generation == myGen else { break }
                await handle(event, for: myGen)
                if event.fatal {
                    // Fatal is a channel close, never a process quit.
                    if event.code == "listening_stopped" {
                        stayMuted = true
                        isMuted = true
                        model.isLiveMuted = true
                        model.player.stop()
                        microphone.stop()
                        microphoneStarted = false
                        AudioInputLease.release(.live)
                        VoiceLevelMeter.shared.resetInput()
                        model.player.bind(to: nil)
                        model.noteLiveMuted()
                    }
                    break
                }
            }
        } catch is CancellationError {
            tearDownChannel(for: myGen)
            throw CancellationError()
        } catch {
            // A transport error (server restart close-frame, socket reset)
            // must NEVER kill the reconnect task — that left Evie deaf until
            // a manual relaunch. Treat it like any other channel loss: tear
            // down and let the loop reconnect.
            let rendered = modelFormatted(error)
            Self.st("ST16_UNEXPECTED_DISCONNECT", rendered)
            tearDownChannel(for: myGen)
        }
        tearDownChannel(for: myGen)
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
        // Reconnect backoff is owned solely by runLoop (900ms); no extra
        // sleep here to avoid stacking 0.4s + 0.9/1.5s.
        }
    }

    @discardableResult
    private func startMicrophone(on connection: LiveVoiceConnection) -> Bool {
        return startMicrophone(on: connection, retryBudget: 1)
    }

    private func micEnqueue(for connection: LiveVoiceConnection) -> @Sendable (Data) -> Void {
        return { [weak self, weak connection] data in
            if !Self.micFirstFrameLogged {
                Self.micFirstFrameLogged = true
                Self.st("ST07_FIRST_MIC_FRAME")
            }
            // Tag planes: MIC_INPUT_PCM (owner) vs ASSISTANT_TTS_PCM (playback)
            // Never allow ambiguous "audio".
            VoiceLevelMeter.shared.ingestInputPCM16(data)
            guard self?.providerReadyForForward == true else { return } // local-only until provider ready
            // SH-3 HARD HALF-DUPLEX GATE: while assistant PCM is physically audible (scheduled + tail),
            // do NOT forward mic PCM to provider. Mic remains RUNNING for level meter, but gate is closed.
            // Uses physical playback truth, not UI status.
            if self?.playbackPlayer?.shouldMuteCapture == true { return }
            connection?.enqueuePCM(data)
        }
    }

    @discardableResult
    private func startMicrophone(on connection: LiveVoiceConnection, retryBudget: Int) -> Bool {
        // MIC STATE MACHINE LAW: RUNNING -> STARTING without STOP is illegal.
        // If already RUNNING with healthy engine and lease, be idempotent
        // but refresh forwarding destination to the new connection (keep-mic-alive
        // must not leave the tap pointing at a closed socket).
        if microphoneStarted, microphone.isRunning, AudioInputLease.currentOwner() == .live {
            microphone.updateEnqueue(micEnqueue(for: connection))
            return true
        }
        // FIX 1: bounded retry with ghost-lease elimination (P0 voice reliability).
        // Hardware format 0Hz/0ch or -10867 no longer leaves a ghost .live lease.
        for attempt in 0...retryBudget {
            microphone.stop()
            microphoneStarted = false
            // Stop capture before changing the playback graph (-10867 safety).
            model?.player.bind(to: nil)
            guard AudioInputLease.acquire(.live) else {
                model?.noteMicrophoneCaptureFailed("already in use")
                return false
            }
            // Pre-flight hardware validation: don't build a graph that is
            // guaranteed to throw -10867. Treat 0Hz as a transient TCC/device
            // settling state and retry with a fresh engine.
            do {
                let hwFormat: AVAudioFormat
                do {
                    hwFormat = try ObjCException.attachAndPrepare(microphone.engine)
                } catch {
                    throw error
                }
                if hwFormat.sampleRate <= 0 || hwFormat.channelCount == 0 {
                    throw NSError(domain: "EVAudio", code: -10867, userInfo: [NSLocalizedDescriptionKey: "microphone input format unavailable (0 Hz)"])
                }
            } catch {
                // Validation threw — release lease before retry so next
                // fresh engine can acquire it.
                AudioInputLease.release(.live)
                if attempt < retryBudget {
                    Self.st("ST06_RETRY", "hwFormat 0Hz attempt \(attempt)")
                    Thread.sleep(forTimeInterval: 0.35)
                    continue
                }
                let ns = error as NSError
                Self.st("ST06_FAILED", "hwFormat 0Hz terminal \(ns.code)")
                model?.noteMicrophoneCaptureFailed("Microphone unavailable — check Permissions")
                microphoneStarted = false
                return false
            }
            do {
                // GOLDEN VOICE PATH (2026-08-23 owner decision): one speech lane,
                // no experiments in the mic tap. Listener Presence is cancelled.
                // The backend's authoritative-playback mic gate owns self-echo.
                // SPOKEN INTERRUPTION CLOSED: the mic tap feeds exactly two
                // consumers — the UI meter and (when provider-ready) the provider.
                try microphone.start(enqueue: micEnqueue(for: connection))
                model?.player.beginVoiceSession()
                isMuted = false
                model?.isLiveMuted = false
                microphoneStarted = true
                if attempt > 0 { Self.st("ST06_RETRY_OK", "attempt \(attempt)") }
                return true
            } catch {
                // FIX 1 LAW: failed start must NOT leave ghost .live lease.
                AudioInputLease.release(.live)
                microphone.stop()
                microphoneStarted = false
                let ns = error as NSError
                let isTCCRetryable = ns.code == -10867 || ns.localizedDescription.contains("0 Hz") || ns.localizedDescription.contains("unavailable")
                if isTCCRetryable, attempt < retryBudget {
                    Self.st("ST06_RETRY", "start failed \(ns.code) attempt \(attempt)")
                    Thread.sleep(forTimeInterval: 0.35)
                    continue
                }
                Self.st("ST06_FAILED", "terminal \(ns.code) \(ns.localizedDescription.prefix(80))")
                if ns.code == -10867 {
                    model?.noteMicrophoneCaptureFailed("Microphone temporarily busy — retrying")
                } else {
                    model?.noteMicrophoneCaptureFailed(error.localizedDescription)
                }
                return false
            }
        }
        return false
    }

    private func handle(_ event: LiveVoiceEvent, for gen: Int? = nil) async {
        if let gen, gen != generation { return }
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
                let responseID = event.providerResponseId ?? "local-\(generation)-\(id)"
                playbackResponseID = responseID
                playbackProviderResponseID = event.providerResponseId
                model.player.beginResponse(responseID)
                model.messages.append(
                    AppModel.ChatMessage(id: id, role: "assistant", text: "", streaming: true)
                )
                model.status = .thinking
                startResponseWatchdog(for: generation)
            }
        case "backchannel":
            cancelResponseWatchdog()
            // Listener/backchannel audio is disabled: one response lane only.
        case "tts_chunk":
            cancelResponseWatchdog()
            model.lastError = nil
            if let text = event.text, !text.isEmpty, let id = assistantID,
               let index = model.messages.firstIndex(where: { $0.id == id }),
               model.messages[index].text.isEmpty {
                model.messages[index].text = text
            }
            guard var responseID = playbackResponseID else { break }
            if let providerID = event.providerResponseId, !providerID.isEmpty {
                if let acceptedProvider = playbackProviderResponseID, acceptedProvider != providerID {
                    break
                }
                if playbackProviderResponseID == nil {
                    model.player.cancelResponse(responseID)
                    responseID = providerID
                    playbackResponseID = providerID
                    playbackProviderResponseID = providerID
                    model.player.beginResponse(providerID)
                }
            }
            if let b64 = event.audioB64, !b64.isEmpty {
                model.player.enqueueBase64PCM(
                    b64,
                    contentType: event.contentType,
                    sampleRate: Double(event.sampleRate ?? 16_000),
                    responseID: responseID,
                    sequence: event.index
                )
            } else if let ref = event.audioRef, !ref.isEmpty {
                do {
                    let data = try await model.client.voiceAudio(ref: ref)
                    model.player.enqueuePCM(
                        data,
                        contentType: event.contentType,
                        sampleRate: Double(event.sampleRate ?? 16_000),
                        responseID: responseID,
                        sequence: event.index
                    )
                } catch {
                    model.lastError = "TTS download failed: \(error.localizedDescription)"
                }
            }
        case "reply":
            cancelResponseWatchdog()
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
            if let responseID = playbackResponseID {
                model.player.finishResponse(responseID)
            }
            if !model.player.isPlaying {
                model.status = .listening
                connection?.sendPlayback(active: false)
            }
        case "barge_in":
            model.player.cancelResponse(playbackResponseID)
            playbackResponseID = nil
            playbackProviderResponseID = nil
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
                providerReadyForForward = false
                Self.st("ST16_PROVIDER_LOST_FORWARD_CLOSED", event.text ?? "")
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

    private static func isBenignRealtimeError(_ message: String) -> Bool {
        let blob = message.lowercased()
        return blob.contains("no active response")
            || blob.contains("cancellation failed")
            || blob.contains("already cancelled")
            || blob.contains("already canceled")
            || blob.contains("already has an active response")
            || blob.contains("active response in progress")
    }

    private func tearDownChannel(for gen: Int? = nil) {
        if let gen, gen != generation { return }
        responseWatchdog?.cancel()
        responseWatchdog = nil
        computerStateTask?.cancel()
        computerStateTask = nil
        removeEscapeStop()
        // RECONNECT LAW: keep local mic engine alive across transient provider/
        // WebSocket reconnects. Only gate forwarding. Physical graph teardown
        // happens only on explicit stop or proven engine failure. This prevents
        // WebSocket reconnect from thrashing the AVAudioEngine.
        providerReadyForForward = false
        model?.player.cancelResponse(playbackResponseID)
        playbackResponseID = nil
        playbackProviderResponseID = nil
        // Keep capture alive — do not touch microphone engine or AudioInputLease.
        // The next connectOnce will reuse the existing running engine via
        // idempotent startMicrophone (checks microphone.isRunning).
        VoiceLevelMeter.shared.resetInput()
        connection?.close()
        connection = nil
    }

    private func fullTeardownCapture() {
        microphone.stop()
        microphoneStarted = false
        VoiceLevelMeter.shared.resetInput()
        model?.player.endVoiceSession()
        AudioInputLease.release(.live)
    }

    private func startResponseWatchdog(for gen: Int) {
        responseWatchdog?.cancel()
        responseWatchdog = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard !Task.isCancelled else { return }
            guard let self, self.generation == gen else { return }
            // Provably broken: thinking with no audio started
            if self.model?.status == .thinking, !(self.model?.player.isPlaying ?? false) {
                Self.st("WDOG_NO_RESPONSE", "10s no response after final_transcript gen \(gen)")
                self.tearDownChannel(for: gen)
            }
        }
    }

    private func cancelResponseWatchdog() {
        responseWatchdog?.cancel()
        responseWatchdog = nil
    }

    private func modelFormatted(_ error: Error) -> String {
        model?.formattedLiveError(error) ?? error.localizedDescription
    }
}
