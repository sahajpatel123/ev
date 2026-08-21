import AppKit
import AVFoundation
import EVAuth
import EVClient
import EVRuntime
import Foundation
import ServiceManagement

@MainActor
final class AppModel: ObservableObject {
    enum Status: String, Sendable {
        case offline
        case listening
        case thinking
        case speaking
    }

    struct ChatMessage: Identifiable, Equatable {
        let id: String
        let role: String
        var text: String
        var streaming: Bool
    }

    struct LiveConfirmationHold: Equatable {
        let action: String
        let target: String?
        let method: String?
        let expiry: String?
    }

    enum BridgeConnectionState: String, Sendable {
        case notChecked
        case permissionRequired
        case connecting
        case authorizationRequired
        case connected
        case helperMissing
        case failed
    }

    struct MacBridgeStatus: Identifiable, Equatable, Sendable {
        let id: String
        let title: String
        var state: BridgeConnectionState
        var detail: String
    }

    @Published var status: Status = .offline
    @Published var captureText = ""
    @Published var messages: [ChatMessage] = []
    @Published var hudCard: HUDCard?
    @Published var queueCount = 0
    @Published var lastError: String?
    @Published var needsComputerAccessibility = false
    @Published var isRecording = false
    @Published var isLiveMuted = false
    @Published var isLiveActive = false
    @Published var isLivePaused = false
    @Published var sessionId: String?
    @Published var conversationId: String?
    @Published var transcript = ""
    @Published var confirmingHud = false
    @Published var cameraState: CameraStateSnapshot = .unknown
    @Published var cameraRequestInFlight = false
    @Published var capabilityManifest = CapabilityManifest()
    @Published var deviceMesh = DeviceMeshSnapshot()
    @Published var activeLiveProvider: String?
    /// Exact client-visible proof for the current/last Mac live runtime.
    /// ``MenuBarView`` and diagnostics tooling can render ``displayText``
    /// without reaching into the socket or duplicating event parsing.
    @Published var liveRuntimeDiagnostics = LiveRuntimeDiagnostics()
    @Published var activeLiveModel: String?
    @Published var liveTTSDeviceID: String?
    @Published var advertisedLiveToolNames: [String] = []
    @Published var providerAcknowledgedToolNames: [String] = []
    @Published var liveToolsReported = false
    @Published var providerToolsReported = false
    @Published var providerSessionReady = false
    @Published var capabilityError: String?
    @Published var lastToolCallName: String?
    @Published var lastToolResultStatus: String?
    @Published var lastToolEvidenceSource: String?
    @Published var lastToolEvidenceTimestamp: String?
    @Published var liveConfirmationHold: LiveConfirmationHold?
    /// Backend bridge state is separate from local TCC permission state.
    @Published var macBridgeStatuses: [MacBridgeStatus] = []

    private(set) var config: AppConfig
    private(set) var client: EVAPIClient
    let queue: OfflineCaptureQueue
    let hotkey = GlobalHotkey()
    let mic = MicCapture()
    let player = TTSPlayer()
    let live = LiveConversation()

    private var heartbeatTask: Task<Void, Never>?
    private var conversationTask: Task<Void, Never>?
    private var recordLimitTask: Task<Void, Never>?
    private var pendingAssistantID: String?
    private var started = false
    private var sendingVoice = false
    private var micAuthObserver: NSObjectProtocol?

    init() {
        let config = AppConfig()
        self.config = config
        client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("EV", isDirectory: true)
        ?? FileManager.default.temporaryDirectory.appendingPathComponent("EV", isDirectory: true)
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        queue = OfflineCaptureQueue(store: FileCaptureQueueStore(directory: support))
        liveRuntimeDiagnostics.setBackend(
            url: config.baseURL,
            source: config.baseURLSource
        )
        Task { @MainActor [weak self] in
            self?.start()
        }
    }

    /// A read-only render target for a future diagnostics panel/log export.
    /// The underlying facts remain structured in ``liveRuntimeDiagnostics``.
    var liveRuntimeDiagnosticsText: String {
        liveRuntimeDiagnostics.displayText
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue {
                    try SMAppService.mainApp.register()
                } else {
                    try SMAppService.mainApp.unregister()
                }
            } catch {
                lastError = "Launch at login: \(error.localizedDescription)"
            }
        }
    }

    @discardableResult
    func reloadAPICredentials() -> Bool {
        if let stored = UserDefaults.standard.string(forKey: "EV_API_KEY"),
           !APIAuthKey.isUsable(stored) {
            UserDefaults.standard.removeObject(forKey: "EV_API_KEY")
        }
        let fresh = AppConfig()
        guard APIAuthKey.isUsable(fresh.apiKey) else { return false }
        let changed = fresh.apiKey != client.token || fresh.baseURL != client.baseURL
        config = fresh
        client = EVAPIClient(baseURL: fresh.baseURL, token: fresh.apiKey)
        liveRuntimeDiagnostics.setBackend(
            url: fresh.baseURL,
            source: fresh.baseURLSource
        )
        if changed, live.isRunning {
            // Keep the loop and its audio ownership intact; closing only the
            // current channel makes the existing loop reconnect through the
            // newly resolved AppConfig client on its normal path.
            live.configurationDidChange()
        }
        return changed
    }

    private func isUnauthorized(_ error: Error) -> Bool {
        if case EVAPIError.httpStatus(401, _) = error { return true }
        return false
    }

    func start() {
        guard !started else { return }
        started = true
        _ = reloadAPICredentials()
        if config.usesPlaceholderKey {
            lastError = "API key is still the placeholder “dev”. EV.app now reads EV_MASTER_KEY from ~/Library/Application Support/EV/api.env, ~/.ev/env, or the repo .env. Rebuild the app after packaging so Talk and chat authenticate."
        }
        live.attach(self)
        VoiceOrbOverlay.shared.attach(self)
        observeMicrophoneAuthorization()
        // ⇧⌘E must be live at app-open. Registering only in MenuBarView.onAppear
        // left the Talk handler uninstalled until the panel was opened once.
        hotkey.start(
            keyCode: 14, // "e"
            flags: [.command, .shift],
            handler: { [weak self] in
                Task { @MainActor in
                    self?.toggleTalk()
                }
            }
        )
        // ev.ears and live cannot share the input. Kill ears before any
        // AVAudioEngine tap or Talk can race it (that abort looked like quit).
        EarsProcess.stopAndWait()
        // Claim the mic before any await. If Talk/hotkey fires during
        // refresh(), a second AVAudioEngine on the same input aborts EV.
        live.start()
        Task {
            await refresh()
            await bootstrapIfNeeded()
            let statuses = await PermissionCenter.statuses()
            await connectGrantedBridges(from: statuses)
        }
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard !Task.isCancelled else { break }
                await self?.tick()
            }
        }
        conversationTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                guard !Task.isCancelled else { break }
                await self?.refreshConversation()
            }
        }
    }

    func tick() async {
        await refreshHealth()
        await refreshDeviceMesh()
        await syncQueue()
        await updateQueueCount()
        await refreshConversation()
    }

    func refresh() async {
        await refreshHealth()
        await refreshDeviceMesh()
        await refreshHUD()
        await syncQueue()
        await updateQueueCount()
        await refreshConversation()
    }

    func bootstrapIfNeeded() async {
        do {
            let registryId = try await ensureRegistryDevice()
            let result = try await client.bootstrapDevice(id: registryId)
            if result.spoken, let line = result.spokenText, !line.isEmpty {
                lastError = nil
            }
            if let liveId = result.prefs?.liveConversationId, !liveId.isEmpty {
                conversationId = liveId
            }
            if result.prefsLoaded == false {
                lastError = result.spokenText ?? "I couldn't load prefs; using defaults."
            }
            await refreshDeviceMesh()
        } catch {
            if isUnauthorized(error) {
                status = .offline
            }
        }
    }

    private func ensureRegistryDevice() async throws -> String {
        let defaults = UserDefaults.standard
        if let stored = defaults.string(forKey: "EV_REGISTRY_DEVICE_ID"),
           UUID(uuidString: stored) != nil {
            return stored
        }
        let created = try await client.createDevice(
            name: config.deviceID,
            capabilities: ["attention", "voice", "camera"],
            deviceType: "mac"
        )
        defaults.set(created.device.id, forKey: "EV_REGISTRY_DEVICE_ID")
        return created.device.id
    }

    func refreshHealth() async {
        do {
            let health = try await client.health()
            capabilityManifest = health.capabilityManifest
            activeLiveProvider = health.providers?["live"]?.stringValue
            liveRuntimeDiagnostics.setBackend(
                url: client.baseURL,
                source: config.baseURLSource
            )
            liveRuntimeDiagnostics.backendStatus = health.status
            liveRuntimeDiagnostics.backendVersion = health.version
            liveRuntimeDiagnostics.backendEnvironment = health.environment
            if let runtime = health.runtime {
                liveRuntimeDiagnostics.backendPID = runtime["pid"]?.numberValue.map(Int.init)
                liveRuntimeDiagnostics.backendStartedAt = runtime["started_at"]?.stringValue
                liveRuntimeDiagnostics.backendSourceFingerprint =
                    runtime["realtime_bridge_source_fingerprint"]?.stringValue
            }
            if let liveProvider = health.providers?["live"]?.objectValue {
                let provider = liveProvider["provider"]?.stringValue
                    ?? liveProvider["name"]?.stringValue
                let model = liveProvider["model"]?.stringValue
                if let provider, !provider.isEmpty {
                    activeLiveProvider = provider
                }
                liveRuntimeDiagnostics.updateRuntime(
                    provider: provider,
                    model: model,
                    advertisedTools: [],
                    providerAcknowledgedTools: [],
                    providerSessionReady: liveRuntimeDiagnostics.providerSessionReady,
                    capabilityErrors: liveRuntimeDiagnostics.capabilityErrors
                )
            }
            if status == .offline {
                status = .listening
            }
            let listener = RuntimeListener(client: client)
            let listenerState = status == .offline ? "off" : "listening"
            _ = try? await listener.heartbeat(deviceID: config.deviceID, listenerState: listenerState)
        } catch {
            if isUnauthorized(error), reloadAPICredentials() {
                await refreshHealth()
                return
            }
            status = .offline
        }
    }

    // MARK: - Live runtime diagnostics

    func noteLiveConnectionAttempt(deviceID: String) {
        liveRuntimeDiagnostics.beginConnectionAttempt(
            backendURL: client.baseURL,
            backendSource: config.baseURLSource,
            deviceID: deviceID
        )
    }

    func noteLiveSessionOpened(sessionID: String, deviceID: String) {
        self.sessionId = sessionID
        liveRuntimeDiagnostics.sessionOpened(sessionID: sessionID, deviceID: deviceID)
    }

    func noteLiveConnected() {
        liveRuntimeDiagnostics.connected()
    }

    func noteLiveMuted() {
        liveRuntimeDiagnostics.muted()
    }

    func noteLiveDisconnected(reason: String, willReconnect: Bool) {
        liveRuntimeDiagnostics.disconnected(reason: reason, willReconnect: willReconnect)
    }

    func noteLiveStopped() {
        liveRuntimeDiagnostics.stopped()
    }

    func noteLiveRuntime(
        provider: String?,
        model: String?,
        advertisedTools: [String],
        providerAcknowledgedTools: [String],
        providerSessionReady: Bool,
        capabilityErrors: [String]
    ) {
        if let provider, !provider.isEmpty {
            activeLiveProvider = provider
        }
        liveRuntimeDiagnostics.updateRuntime(
            provider: provider,
            model: model,
            advertisedTools: advertisedTools,
            providerAcknowledgedTools: providerAcknowledgedTools,
            providerSessionReady: providerSessionReady,
            capabilityErrors: capabilityErrors
        )
    }

    func noteLiveCapabilityError(_ error: String) {
        liveRuntimeDiagnostics.addCapabilityError(error)
    }

    /// Consume the existing HUD proof fields. This keeps tool argument values
    /// out of diagnostics while preserving the call/result/evidence chain.
    func noteLiveToolHUD(_ card: HUDCard) {
        guard let meta = card.meta else { return }
        let kind = card.metaKind ?? ""
        guard let name = meta["tool"]?.stringValue, !name.isEmpty else { return }
        let observedAt = card.generatedAt.isEmpty ? nil : card.generatedAt
        let argumentKeys = meta["arguments"]?.objectValue.map { $0.keys.sorted() } ?? []
        let callID = meta["_realtime_call_id"]?.stringValue
        if kind == "progress" || kind == "approval_hold" {
            liveRuntimeDiagnostics.recordToolCall(
                LiveRuntimeToolCall(
                    name: name,
                    callID: callID,
                    argumentKeys: argumentKeys,
                    observedAt: observedAt
                )
            )
            return
        }

        let success = meta["success"]?.boolValue ?? (kind == "evidence")
        let verified = meta["verified"]?.boolValue ?? (kind == "evidence")
        // The HUD body and backend error can contain message text or targets.
        // Keep only a boolean outcome in the diagnostics projection.
        let error = meta["error"]?.stringValue.map { _ in "tool reported failure" }
        liveRuntimeDiagnostics.recordToolResult(
            LiveRuntimeToolResult(
                name: name,
                success: success,
                verified: verified,
                summary: "",
                error: error,
                observedAt: observedAt
            )
        )
        if kind == "evidence" {
            let evidence = meta["evidence"]?.objectValue ?? [:]
            let source = evidence["source"]?.stringValue
                ?? meta["source"]?.stringValue
                ?? name
            let timestamp = evidence["timestamp"]?.stringValue
                ?? meta["timestamp"]?.stringValue
            liveRuntimeDiagnostics.recordEvidence(
                LiveRuntimeEvidence(
                    source: source,
                    timestamp: timestamp,
                    summary: ""
                )
            )
        }
    }

    func resetLiveDiagnostics() {
        activeLiveModel = nil
        liveTTSDeviceID = nil
        advertisedLiveToolNames = []
        providerAcknowledgedToolNames = []
        liveToolsReported = false
        providerToolsReported = false
        providerSessionReady = false
        capabilityError = nil
        lastToolCallName = nil
        lastToolResultStatus = nil
        lastToolEvidenceSource = nil
        lastToolEvidenceTimestamp = nil
        liveConfirmationHold = nil
    }

    func applyLiveDiagnostics(
        provider: String? = nil,
        model: String? = nil,
        ttsDeviceID: String? = nil,
        advertisedTools: [String]? = nil,
        acknowledgedTools: [String]? = nil,
        toolsReported: Bool = false,
        providerToolsReported: Bool = false,
        providerSessionReady: Bool? = nil,
        capabilityError: String? = nil
    ) {
        if let provider, !provider.isEmpty {
            activeLiveProvider = provider
        }
        if let model, !model.isEmpty {
            activeLiveModel = model
        }
        if let ttsDeviceID, !ttsDeviceID.isEmpty {
            liveTTSDeviceID = ttsDeviceID
        }
        if let advertisedTools {
            advertisedLiveToolNames = advertisedTools
        }
        if let acknowledgedTools {
            providerAcknowledgedToolNames = acknowledgedTools
        }
        liveToolsReported = liveToolsReported || toolsReported
        self.providerToolsReported = self.providerToolsReported || providerToolsReported
        if let providerSessionReady {
            self.providerSessionReady = providerSessionReady
        }
        if let capabilityError, !capabilityError.isEmpty {
            self.capabilityError = capabilityError
        }
    }

    func noteProviderToolMismatch(acknowledgedTools: [String], message: String) {
        providerAcknowledgedToolNames = acknowledgedTools
        providerToolsReported = true
        providerSessionReady = true
        if !message.isEmpty {
            capabilityError = message
        }
        liveRuntimeDiagnostics.updateRuntime(
            provider: liveRuntimeDiagnostics.provider,
            model: liveRuntimeDiagnostics.model,
            advertisedTools: liveRuntimeDiagnostics.advertisedTools,
            providerAcknowledgedTools: acknowledgedTools,
            providerSessionReady: true,
            capabilityErrors: message.isEmpty
                ? liveRuntimeDiagnostics.capabilityErrors
                : liveRuntimeDiagnostics.capabilityErrors + [message]
        )
    }

    func noteLiveToolProgress(name: String) {
        lastToolCallName = name.isEmpty ? nil : name
        lastToolResultStatus = name.isEmpty ? nil : "in progress"
        lastToolEvidenceSource = nil
        lastToolEvidenceTimestamp = nil
        liveConfirmationHold = nil
    }

    func noteLiveConfirmationHold(
        action: String,
        target: String?,
        method: String?,
        expiry: String?
    ) {
        lastToolCallName = action.isEmpty ? nil : action
        lastToolResultStatus = action.isEmpty ? nil : "waiting for confirmation"
        lastToolEvidenceSource = nil
        lastToolEvidenceTimestamp = nil
        liveConfirmationHold = LiveConfirmationHold(
            action: action,
            target: target,
            method: method,
            expiry: expiry
        )
    }

    func noteLiveToolResult(name: String, success: Bool) {
        lastToolCallName = name.isEmpty ? lastToolCallName : name
        lastToolResultStatus = success ? "success" : "failed"
        liveConfirmationHold = nil
        lastToolEvidenceSource = nil
        lastToolEvidenceTimestamp = nil
    }

    func noteLiveToolEvidence(name: String, source: String?, timestamp: String?) {
        lastToolCallName = name.isEmpty ? lastToolCallName : name
        lastToolResultStatus = "success · evidence reported"
        lastToolEvidenceSource = source
        lastToolEvidenceTimestamp = timestamp
        liveConfirmationHold = nil
    }

    // MARK: - Permission-to-backend bridges

    func bridgeStatus(for id: String) -> MacBridgeStatus? {
        macBridgeStatuses.first { $0.id == id }
    }

    /// Connect every bridge whose local permission is already granted. TCC
    /// permission is reported first; these calls then perform the real backend
    /// integration setup. No local calendar or fake messaging provider is
    /// created when the backend/helper is unavailable.
    ///
    /// Automatic startup never opens a Google OAuth browser. Calendar stays
    /// connected when OAuth is already authorized; otherwise it waits.
    func connectGrantedBridges(
        from statuses: [PermissionStatus],
        openCalendarAuthorization: Bool = false
    ) async {
        let calendarGranted = statuses.contains {
            $0.kind == .calendars && $0.state == .granted
        }
        let lifeGranted = statuses.contains {
            $0.kind == .automation && ($0.state == .granted || $0.state == .partial)
        }
        if calendarGranted {
            await connectGoogleCalendarBridge(openAuthorization: openCalendarAuthorization)
        } else {
            setBridge(
                id: "google-calendar",
                title: "Google Calendar",
                state: .permissionRequired,
                detail: "macOS Calendar permission is not granted yet."
            )
        }
        if lifeGranted {
            await connectMacOSLifeBridges()
        } else {
            for spec in Self.lifeBridgeSpecs {
                setBridge(
                    id: spec.id,
                    title: spec.title,
                    state: .permissionRequired,
                    detail: "macOS Automation permission is not granted yet."
                )
            }
        }
    }

    private func connectGoogleCalendarBridge(openAuthorization: Bool) async {
        setBridge(
            id: "google-calendar",
            title: "Google Calendar",
            state: .connecting,
            detail: "Checking the real Google Calendar adapter…"
        )
        do {
            let integration = try await findOrInstallIntegration(
                adapter: "calendar",
                slug: "google-calendar",
                name: "Google Calendar",
                scopes: ["calendar:read"],
                config: ["provider": .string("google")]
            )
            if integration.credentialConfigured {
                let oauthStatus = try await client.integrationOAuthStatus(integrationID: integration.id)
                if oauthStatus.authorized {
                    setBridge(
                        id: "google-calendar",
                        title: "Google Calendar",
                        state: .connected,
                        detail: "macOS allowed Calendar. Google Calendar is connected."
                    )
                    return
                }
            }
            if !openAuthorization {
                setBridge(
                    id: "google-calendar",
                    title: "Google Calendar",
                    state: .authorizationRequired,
                    detail: "macOS allowed Calendar. Google Calendar still needs OAuth."
                )
                return
            }
            let authorization = try await client.beginIntegrationOAuth(integrationID: integration.id)
            guard let url = URL(string: authorization.authorizeURL) else {
                throw EVAPIError.decoding("Google OAuth returned an invalid authorization URL")
            }
            NSWorkspace.shared.open(url)
            setBridge(
                id: "google-calendar",
                title: "Google Calendar",
                state: .authorizationRequired,
                detail: "macOS allowed Calendar. I still need Google Calendar connected. I can start that now."
            )
        } catch {
            setBridge(
                id: "google-calendar",
                title: "Google Calendar",
                state: .failed,
                detail: formattedAPIError(error, fallback: "Google Calendar connection failed")
            )
        }
    }

    private func connectMacOSLifeBridges() async {
        guard let helperPath = Self.lifeHelperPath() else {
            for spec in Self.lifeBridgeSpecs {
                setBridge(
                    id: spec.id,
                    title: spec.title,
                    state: .helperMissing,
                    detail: "macOS allowed the local app permission, but EVLifeHelper is missing."
                )
            }
            return
        }
        for spec in Self.lifeBridgeSpecs {
            setBridge(
                id: spec.id,
                title: spec.title,
                state: .connecting,
                detail: "Connecting the real macos_life adapter…"
            )
            do {
                let integration = try await findOrInstallIntegration(
                    adapter: spec.adapter,
                    slug: spec.id,
                    name: spec.title,
                    scopes: spec.scopes,
                    config: [
                        "provider": .string("macos_life"),
                        "helper_path": .string(helperPath),
                    ]
                )
                setBridge(
                    id: spec.id,
                    title: spec.title,
                    state: .connected,
                    detail: "macOS permission reported. Backend macos_life bridge connected."
                )
                _ = integration
            } catch {
                setBridge(
                    id: spec.id,
                    title: spec.title,
                    state: .failed,
                    detail: formattedAPIError(error, fallback: "\(spec.title) bridge connection failed")
                )
            }
        }
    }

    private func findOrInstallIntegration(
        adapter: String,
        slug: String,
        name: String,
        scopes: [String],
        config: [String: AnyCodable]
    ) async throws -> IntegrationRecord {
        let existing = try await client.integrations(includeRevoked: true)
            .first { $0.slug == slug && $0.adapter == adapter }
        if let existing, existing.status == "active" {
            return existing
        }
        return try await client.installIntegration(
            adapter: adapter,
            slug: slug,
            name: name,
            scopes: scopes,
            config: config
        )
    }

    private func setBridge(
        id: String,
        title: String,
        state: BridgeConnectionState,
        detail: String
    ) {
        if let index = macBridgeStatuses.firstIndex(where: { $0.id == id }) {
            macBridgeStatuses[index] = MacBridgeStatus(id: id, title: title, state: state, detail: detail)
        } else {
            macBridgeStatuses.append(MacBridgeStatus(id: id, title: title, state: state, detail: detail))
        }
    }

    private struct LifeBridgeSpec: Sendable {
        let id: String
        let title: String
        let adapter: String
        let scopes: [String]
    }

    /// Resolve the real helper shipped beside EV.app (or the development
    /// sibling produced by SwiftPM). Returning nil is intentional: callers
    /// must report the missing bridge instead of creating a local fake.
    private static func lifeHelperPath() -> String? {
        var candidates: [String] = []
        if let configured = ProcessInfo.processInfo.environment["EV_LIFE_HELPER_PATH"],
           !configured.isEmpty {
            candidates.append(configured)
        }
        if let executable = Bundle.main.executableURL {
            candidates.append(executable.deletingLastPathComponent().appendingPathComponent("EVLifeHelper").path)
        }
        candidates.append(
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
                .appendingPathComponent(".build/debug/EVLifeHelper")
                .path
        )
        var seen = Set<String>()
        return candidates.first { path in
            seen.insert(path).inserted && FileManager.default.isExecutableFile(atPath: path)
        }
    }

    private static let lifeBridgeSpecs = [
        LifeBridgeSpec(id: "macos-messaging", title: "Messages", adapter: "messaging", scopes: ["messaging:read", "messaging:act"]),
        LifeBridgeSpec(id: "macos-phone", title: "Calls", adapter: "phone", scopes: ["phone:act"]),
        LifeBridgeSpec(id: "macos-mail", title: "Mail", adapter: "mail", scopes: ["mail:read", "mail:act"]),
    ]

    func refreshDeviceMesh() async {
        do {
            let sync = try await client.runtimeSync()
            // Replace, rather than merge, so revoked/disappeared nodes are
            // removed from the UI as soon as the registry reports them gone.
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
            // A transient sync failure is not evidence that devices vanished.
            // Keep the last confirmed snapshot and let the next heartbeat retry.
        }
    }

    func refreshHUD(force: Bool = false) async {
        // Live HUD cards arrive on the voice socket. Skip the poll so a
        // progress/hold card is not overwritten by /v1/hud/card — unless a
        // HUD tap just confirmed and we need the latest cached card.
        if !force, isLiveActive || isLiveMuted { return }
        hudCard = try? await client.hudCard()
    }

    func confirmHudAction() {
        guard !confirmingHud, let card = hudCard, card.isApprovalHold,
              let name = card.holdToolName, !name.isEmpty else { return }
        confirmingHud = true
        Task { @MainActor [weak self] in
            guard let self else { return }
            defer { confirmingHud = false }
            if EVLifeBiometric.isAvailable {
                let ok = await EVLifeBiometric.confirmLifeAction(
                    reason: "Confirm \(name.replacingOccurrences(of: "_", with: " "))"
                )
                guard ok else {
                    lastError = "Confirmation cancelled"
                    return
                }
            }
            do {
                if let actionId = card.holdActionId, !actionId.isEmpty {
                    let proof = try? await client.issueReverification(
                        purpose: "runtime.action",
                        voiceSessionId: sessionId
                    )
                    let response = try await client.approveAction(
                        id: actionId,
                        reverifyToken: proof?.token
                    )
                    if response.status == "executed" || response.status == "approved" {
                        lastError = nil
                        await refreshHUD(force: true)
                    } else {
                        lastError = response.error ?? "Confirmation failed"
                    }
                    return
                }
                var arguments = card.holdArguments
                arguments["confirm"] = true
                let response = try await client.dispatchTool(
                    name: name,
                    arguments: arguments,
                    confirm: true,
                    allowSensitive: true
                )
                if response.ok {
                    lastError = nil
                    await refreshHUD(force: true)
                } else {
                    lastError = response.error ?? "Confirmation failed"
                }
            } catch {
                lastError = formattedAPIError(error, fallback: "Confirmation failed")
            }
        }
    }

    func refreshConversation() async {
        guard !isLiveActive, !isLiveMuted else { return }
        guard pendingAssistantID == nil, !isRecording, !sendingVoice,
              status != .thinking, status != .speaking else { return }
        do {
            let detail = try await client.conversation(limit: 40)
            let mapped = detail.messages.map { message in
                ChatMessage(id: message.id, role: message.role, text: message.text, streaming: false)
            }
            if mapped != messages {
                messages = mapped
            }
            if lastError?.contains("401") == true || lastError?.contains("placeholder") == true {
                lastError = nil
            }
        } catch {
            if isUnauthorized(error), reloadAPICredentials() {
                await refreshConversation()
                return
            }
            if case EVAPIError.httpStatus(401, let body) = error {
                lastError = authFailureMessage(body)
            }
        }
    }

    func syncQueue() async {
        let summary = await queue.sync(using: client)
        if !summary.errors.isEmpty {
            lastError = summary.errors.first
        }
        await updateQueueCount()
    }

    func updateQueueCount() async {
        queueCount = (try? queue.pending().count) ?? 0
    }

    // MARK: - Capture

    func capture() {
        let text = captureText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        captureText = ""
        let payload = CapturePayload(
            source: "mac",
            eventType: "note",
            text: text,
            deviceID: config.deviceID
        )
        Task {
            do {
                let result = try await client.capture(payload: payload)
                lastError = result.duplicate ? "Duplicate capture — already stored." : nil
            } catch EVAPIError.transport {
                do {
                    _ = try queue.enqueue(payload)
                    lastError = "Offline — capture queued for sync."
                } catch {
                    lastError = "Queue write failed: \(error)"
                }
            } catch {
                lastError = formattedAPIError(error, fallback: "Capture failed")
            }
            await updateQueueCount()
        }
    }

    // MARK: - Streaming chat

    func sendChat(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        messages.append(ChatMessage(id: UUID().uuidString, role: "user", text: trimmed, streaming: false))
        let id = UUID().uuidString
        pendingAssistantID = id
        messages.append(ChatMessage(id: id, role: "assistant", text: "", streaming: true))
        status = .thinking

        Task {
            do {
                for try await event in client.askStream(
                    trimmed,
                    deviceId: config.deviceID
                ) {
                    switch event {
                    case .status:
                        status = .thinking
                    case .delta(let chunk, _):
                        if let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].text += chunk
                        }
                    case .refined(let text):
                        if let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].text = text
                        }
                    case .error(let message):
                        lastError = message
                    case .done:
                        if let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].streaming = false
                        }
                        status = .listening
                    default:
                        break
                    }
                }
            } catch {
                lastError = formattedAPIError(error, fallback: "Chat failed")
                if let index = messages.firstIndex(where: { $0.id == id }) {
                    messages[index].streaming = false
                }
                status = .listening
            }
            pendingAssistantID = nil
        }
    }

    // MARK: - Voice (live duplex while the app is open; PTT is fallback)

    func toggleTalk() {
        // Live owns the mic while the app is open. Never start the clip
        // recorder on top of it — two AVAudioEngines on one input abort
        // the process, which looked like “Talk closes the app”.
        switch TalkRouting.action(
            liveOwnsInput: TalkRouting.liveOwnsInput(
                isLiveActive: isLiveActive,
                isLiveMuted: isLiveMuted,
                liveIsRunning: live.isRunning
            ),
            isRecording: isRecording,
            sendingVoice: sendingVoice
        ) {
        case .toggleLiveMute:
            live.toggleMute()
        case .stopClipCapture:
            stopAndSend()
        case .ignore:
            return
        case .startClipCapture:
            startRecording()
        }
    }

    func toggleCamera() {
        live.toggleCamera()
    }

    /// Keep camera UI truthful when the local permission layer reports a
    /// denial before a provider can publish its normal state event.
    func localCameraPermissionState() -> CameraState? {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .denied, .restricted:
            return .denied
        case .authorized:
            return nil
        case .notDetermined:
            return .unknown
        @unknown default:
            return .unknown
        }
    }

    func noteMicrophoneDenied() {
        lastError = "Microphone permission denied — open EV → Permissions for the fix."
        // Health owns offline vs listening. Never mark offline just because
        // capture was treated as denied.
        if status == .offline {
            Task { await refreshHealth() }
        }
    }

    func noteMicrophoneCaptureFailed(_ detail: String? = nil) {
        if let detail, !detail.isEmpty {
            lastError = "Microphone capture failed: \(detail)"
        } else {
            lastError = "Microphone capture failed. Try Talk again."
        }
        if status == .offline {
            Task { await refreshHealth() }
        }
    }

    private func observeMicrophoneAuthorization() {
        if micAuthObserver != nil { return }
        micAuthObserver = NotificationCenter.default.addObserver(
            forName: MicrophoneAuthorization.didChange,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.microphoneAuthorizationDidChange()
            }
        }
    }

    private func microphoneAuthorizationDidChange() {
        guard MicrophoneAuthorization.current() == .granted else { return }
        if lastError?.localizedCaseInsensitiveContains("microphone permission") == true {
            lastError = nil
        }
        if !live.isRunning {
            live.start()
        }
        if status == .offline {
            Task { await refreshHealth() }
        }
    }

    func formattedLiveError(_ error: Error) -> String {
        formattedAPIError(error, fallback: "Live listen failed")
    }

    func startRecording() {
        if TalkRouting.liveOwnsInput(
            isLiveActive: isLiveActive,
            isLiveMuted: isLiveMuted,
            liveIsRunning: live.isRunning
        ) {
            return
        }
        Task {
            let started = await mic.start()
            if TalkRouting.liveOwnsInput(
                isLiveActive: isLiveActive,
                isLiveMuted: isLiveMuted,
                liveIsRunning: live.isRunning
            ) {
                if started {
                    _ = mic.stop()
                }
                return
            }
            isRecording = started
            status = started ? .listening : status
            if !started {
                if MicrophoneAuthorization.current().isUsable {
                    noteMicrophoneCaptureFailed()
                } else {
                    noteMicrophoneDenied()
                }
                return
            }
            lastError = nil
            recordLimitTask?.cancel()
            recordLimitTask = Task { [weak self] in
                let nanos = UInt64(MicCapture.maxSeconds * 1_000_000_000)
                try? await Task.sleep(nanoseconds: nanos)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self?.stopAndSend()
                }
            }
        }
    }

    func stopAndSend() {
        recordLimitTask?.cancel()
        recordLimitTask = nil
        Task {
            let data = mic.stop()
            isRecording = false
            guard let data, !data.isEmpty else {
                if !sendingVoice {
                    status = .listening
                    lastError = "I didn’t hear anything. Hold Push to talk and try again."
                }
                return
            }
            if MicCapture.isQuiet(data) {
                status = .listening
                lastError = "That clip was too quiet. Hold Push to talk closer to the mic."
                return
            }
            let audioB64 = data.base64EncodedString()
            sendingVoice = true
            status = .thinking
            lastError = nil
            defer { sendingVoice = false }
            var attempt = 0
            while attempt < 2 {
                attempt += 1
                do {
                    // Reuse an already-open Talk session. A second press must not
                    // end the follow-up and start a new wake cycle — unless the
                    // held id is already ENDED (SSE "wake EVIE again" loop).
                    guard let session = try await openTalkSession(audioB64: audioB64) else {
                        lastError = "No voice session — grant voice consent in Permissions."
                        status = .listening
                        return
                    }
                    var streamedAudio = false
                    var assistantID: String?
                    var deadSession = false
                    for try await event in client.streamUtterance(
                        sessionId: session,
                        audioB64: audioB64,
                        pushToTalk: true
                    ) {
                        switch event {
                        case .partial:
                            break
                        case .transcript(let spoken):
                            transcript = spoken.text
                            if !spoken.text.isEmpty {
                                messages.append(ChatMessage(id: UUID().uuidString, role: "user", text: spoken.text, streaming: false))
                            }
                            let id = UUID().uuidString
                            assistantID = id
                            messages.append(ChatMessage(id: id, role: "assistant", text: "", streaming: true))
                        case .ttsChunk(let chunk):
                            // One speaker: play TTS bytes only. Never /usr/bin/say
                            // alongside real audio, and never say() a text chunk
                            // that later arrives as TTS.
                            if let b64 = chunk.audioB64, let data = Data(base64Encoded: b64), !data.isEmpty {
                                streamedAudio = true
                                status = .speaking
                                try? player.enqueue(data)
                            }
                        case .reply(let response):
                            if let id = assistantID, let index = messages.firstIndex(where: { $0.id == id }) {
                                messages[index].text = response.reply
                                messages[index].streaming = false
                            } else {
                                messages.append(ChatMessage(id: UUID().uuidString, role: "assistant", text: response.reply, streaming: false))
                            }
                            if response.state == "ended" {
                                sessionId = nil
                            }
                            if let err = response.error, !err.isEmpty {
                                lastError = err
                            }
                            if !streamedAudio {
                                await playReply(response)
                            }
                        case .error(let message):
                            if attempt == 1 && isDeadVoiceSession(message) {
                                sessionId = nil
                                deadSession = true
                                break
                            }
                            lastError = message
                        case .done:
                            break
                        }
                    }
                    if deadSession {
                        continue
                    }
                    if let id = assistantID, let index = messages.firstIndex(where: { $0.id == id }) {
                        messages[index].streaming = false
                    }
                    status = streamedAudio && player.isPlaying ? .speaking : .listening
                    return
                } catch {
                    let rendered = formattedAPIError(error, fallback: "Voice failed")
                    if attempt == 1 && isDeadVoiceSession(rendered) {
                        sessionId = nil
                        continue
                    }
                    sessionId = nil
                    lastError = rendered
                    status = .listening
                    return
                }
            }
            status = .listening
            if lastError == nil {
                lastError = "Voice session ended — wake EVIE again"
            }
        }
    }

    private func isDeadVoiceSession(_ message: String) -> Bool {
        let lower = message.lowercased()
        return lower.contains("wake evie again")
            || lower.contains("session ended")
            || lower.contains("session_ended")
            || lower.contains("session not found")
            || lower.contains("not verified")
    }

    private func openTalkSession(audioB64: String) async throws -> String? {
        if let existing = sessionId {
            return existing
        }
        // Wake is session setup only — do not upload the spoken clip
        // twice (that doubled Whisper work and blew the 60s timeout).
        let wake = try await client.wakeVoice(
            deviceId: config.deviceID,
            pushToTalk: true
        )
        sessionId = wake.sessionId
        guard let session = wake.sessionId else {
            return nil
        }
        if wake.state == "verifying", wake.ownerEnrolled, let nonce = wake.challengeNonce {
            let verify = try await client.verifyVoice(
                sessionId: session,
                nonce: nonce,
                phrase: wake.challengePhrase,
                samples: [audioB64]
            )
            if !verify.verified {
                lastError = "Speaker verification failed: \(verify.reason)"
                sessionId = nil
                return nil
            }
        }
        return session
    }

    func playReply(_ response: VoiceUtteranceResponse) async {
        status = .speaking
        // decide_playback: TTS audio/ref → play once; never /usr/bin/say
        // as a parallel voice. No audio → stay silent (overlay/text only).
        if let b64 = response.tts?.audioB64, let data = Data(base64Encoded: b64), !data.isEmpty {
            do {
                try player.play(data: data)
                return
            } catch {
                lastError = "TTS playback failed: \(error.localizedDescription)"
            }
        }
        if let ref = response.tts?.audioRef, !ref.isEmpty {
            do {
                let data = try await client.voiceAudio(ref: ref)
                try player.play(data: data)
                return
            } catch {
                lastError = "TTS playback failed: \(error.localizedDescription)"
            }
        }
    }

    func playAudio(ref: String) async {
        do {
            let data = try await client.voiceAudio(ref: ref)
            try player.play(data: data)
        } catch {
            lastError = "TTS playback failed: \(error.localizedDescription)"
        }
    }

    private func speakFallback(_ text: String) {
        let spoken = String(text.prefix(280))
        guard !spoken.isEmpty else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        proc.arguments = ["-v", "Ava", "-r", "160", spoken]
        try? proc.run()
    }

    private func formattedAPIError(_ error: Error, fallback: String) -> String {
        if let apiError = error as? EVAPIError {
            switch apiError {
            case .httpStatus(401, let body):
                return authFailureMessage(body)
            case .httpStatus(let code, let body):
                let detail = body.trimmingCharacters(in: .whitespacesAndNewlines)
                if detail.isEmpty {
                    return "\(fallback): API error \(code)."
                }
                return "\(fallback): \(detail.prefix(240))"
            case .transport(let message) where message.lowercased().contains("timed out"):
                return "\(fallback): the reply took too long to arrive. The live session is still available; try again when ready."
            case .transport(let message):
                return "\(fallback): network error — \(message)"
            case .decoding(let message):
                return "\(fallback): bad reply — \(message)"
            }
        }
        return "\(fallback): \(error.localizedDescription)"
    }

    /// Mirrors ``AppConfig``'s URL precedence for diagnostics only. The
    /// resolved ``client.baseURL`` remains the authority; this labels which
    /// input supplied it without changing configuration resolution.
    private static func backendURLConfigurationSource() -> String {
        let environment = ProcessInfo.processInfo.environment
        if environment["EV_API_URL"] != nil {
            return "AppConfig: EV_API_URL environment"
        }
        if UserDefaults.standard.string(forKey: "EV_API_URL") != nil {
            return "AppConfig: EV_API_URL UserDefaults"
        }

        let fileManager = FileManager.default
        let home = fileManager.homeDirectoryForCurrentUser
        var urls: [URL] = [
            home.appendingPathComponent("Library/Application Support/EV/api.env"),
            home.appendingPathComponent("Library/Application Support/EV/.env"),
            home.appendingPathComponent(".ev/env"),
            home.appendingPathComponent(".ev/.env"),
            home.appendingPathComponent("Code/ev/.env"),
        ]
        var directory = URL(fileURLWithPath: fileManager.currentDirectoryPath)
        for _ in 0..<6 {
            urls.append(directory.appendingPathComponent(".env"))
            directory.deleteLastPathComponent()
        }
        if let executable = Bundle.main.executableURL {
            var parent = executable.deletingLastPathComponent()
            for _ in 0..<8 {
                parent.deleteLastPathComponent()
                urls.append(parent.appendingPathComponent(".env"))
            }
        }
        var seen = Set<String>()
        for url in urls where seen.insert(url.path).inserted {
            guard let text = try? String(contentsOf: url, encoding: .utf8) else { continue }
            let hasURL = text.split(whereSeparator: \.isNewline).contains { rawLine in
                var line = String(rawLine).trimmingCharacters(in: .whitespaces)
                if line.hasPrefix("export ") {
                    line = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
                }
                return line.hasPrefix("EV_API_URL=")
            }
            if hasURL {
                return "AppConfig: dotenv \(url.path)"
            }
        }
        return "AppConfig: built-in default"
    }

    private func authFailureMessage(_ body: String) -> String {
        let detail = body.isEmpty ? "invalid or revoked device token" : body
        return "API rejected this Mac’s key (\(detail)). EV.app must use the same EV_MASTER_KEY as the API — package.sh writes it to ~/Library/Application Support/EV/api.env."
    }
}
