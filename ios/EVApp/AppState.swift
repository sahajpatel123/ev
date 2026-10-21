import EVClient
import Foundation

@MainActor
final class AppState: ObservableObject {
    let client: EVAPIClient
    let queue: OfflineCaptureQueue
    let live = LiveVoiceCoordinator()
    /// Flag-gated wiring; nil unless enabled. Defaults preserve current behavior.
    let connectivity: EvieConnectivityMonitor?
    let sync: EvieEventSync?
    let relayConfig: EvieRelayConfig
    private var connectivityToken: UUID?
    @Published var liveConversationId: String?
    @Published var registryDeviceId: String

    init() {
        let config = AppConfig()
        let clientConfig = EVClientAppConfig()
        // Relay route selection via resolvedBaseURL: direct unless EV_RELAY_ENABLED.
        relayConfig = EvieRelayConfig(enabled: clientConfig.relayEnabled, directBaseURL: clientConfig.baseURL)
        let routedURL = clientConfig.resolvedBaseURL()
        // Gateway-auth cutover path: opt-in via EV_GATEWAY_AUTH_ENABLED; falls back to legacy apiKey.
        let token: String
        if config.gatewayAuthEnabled,
           let gateway = try? EvieGatewayAuth(baseURL: config.baseURL).loadFromKeychain(),
           !gateway.accessToken.isEmpty {
            token = gateway.accessToken
        } else {
            token = config.apiKey
        }
        client = EVAPIClient(baseURL: routedURL, token: token)
        registryDeviceId = UserDefaults.standard.string(forKey: "EV_REGISTRY_DEVICE_ID") ?? config.deviceID

        // Sync drain + connectivity monitor: opt-in via EV_SYNC_DRAIN_ENABLED.
        // The monitor subscription drains on regain of reachability; flags off = nil, no-op.
        if config.syncDrainEnabled {
            let monitor = EvieConnectivityMonitor()
            let engine = EvieEventSync(client: client)
            connectivity = monitor
            sync = engine
            connectivityToken = monitor.subscribe { [weak monitor, weak engine] reachability in
                guard reachability.isReachable else { return }
                Task { _ = try? await engine?.pullToQuiescence(apply: { _ in }) }
                _ = monitor
            }
        } else {
            connectivity = nil
            sync = nil
        }

        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("EVQueue", isDirectory: true)
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("EVQueue", isDirectory: true)
        try? FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        queue = OfflineCaptureQueue(store: FileCaptureQueueStore(directory: documents))
        liveConversationId = UserDefaults.standard.string(forKey: "EV_LIVE_CONVERSATION_ID")
    }

    /// Launch/foreground sync drain. No-op unless EV_SYNC_DRAIN_ENABLED.
    func drainSyncIfEnabled(apply: (@Sendable ([EvieSyncEvent]) async throws -> Void)? = nil) async {
        guard let sync else { return }
        _ = try? await sync.pullToQuiescence(apply: apply ?? { _ in })
    }

    func bootstrapIfNeeded() async {
        let defaults = UserDefaults.standard
        let stored = defaults.string(forKey: "EV_REGISTRY_DEVICE_ID")
        let registryId: String
        if let stored, UUID(uuidString: stored) != nil {
            registryId = stored
        } else {
            do {
                let created = try await client.createDevice(
                    name: AppConfig().deviceID,
                    capabilities: ["attention", "voice"],
                    deviceType: "phone",
                    clientDeviceId: AppConfig().deviceID
                )
                defaults.set(created.device.id, forKey: "EV_REGISTRY_DEVICE_ID")
                registryId = created.device.id
            } catch {
                return
            }
        }
        registryDeviceId = registryId
        do {
            let result = try await client.bootstrapDevice(id: registryId)
            if let id = result.prefs?.liveConversationId, !id.isEmpty {
                liveConversationId = id
                defaults.set(id, forKey: "EV_LIVE_CONVERSATION_ID")
            }
        } catch {
            return
        }
    }

    func startLiveIfPossible() {
        // Relay voice path only when EV_RELAY_ENABLED; otherwise direct (unchanged).
        if relayConfig.enabled {
            live.configureLiveRelay(config: relayConfig, decision: nil, provider: nil)
        }
        live.start(client: client, deviceId: registryDeviceId)
    }

    func noteConversation(_ id: String?) {
        guard let id, !id.isEmpty else { return }
        liveConversationId = id
        UserDefaults.standard.set(id, forKey: "EV_LIVE_CONVERSATION_ID")
    }

    func startHealthBridge() async {
        do {
            try await HealthKitManager.shared.requestReadAccess()
            await HealthKitManager.shared.enableBackgroundDelivery()
            await HealthKitManager.shared.publish(
                using: client,
                deviceId: registryDeviceId
            )
        } catch {
            // Health is optional — the rest of EV stays up if the owner declines.
        }
    }
}
// Cycle EAC-01 — Evie Today-state snapshot.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
struct EvieTodayState: Equatable, Sendable {
    var title: String
    var detail: String
    var pendingCount: Int

    static let empty = EvieTodayState(title: "", detail: "", pendingCount: 0)

    init(title: String = "", detail: String = "", pendingCount: Int = 0) {
        self.title = title
        self.detail = detail
        self.pendingCount = pendingCount
    }
}
