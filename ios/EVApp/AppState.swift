import EVClient
import Foundation

@MainActor
final class AppState: ObservableObject {
    let client: EVAPIClient
    let queue: OfflineCaptureQueue
    let live = LiveVoiceCoordinator()
    @Published var liveConversationId: String?
    @Published var registryDeviceId: String

    init() {
        let config = AppConfig()
        client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        registryDeviceId = UserDefaults.standard.string(forKey: "EV_REGISTRY_DEVICE_ID") ?? config.deviceID

        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("EVQueue", isDirectory: true)
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("EVQueue", isDirectory: true)
        try? FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        queue = OfflineCaptureQueue(store: FileCaptureQueueStore(directory: documents))
        liveConversationId = UserDefaults.standard.string(forKey: "EV_LIVE_CONVERSATION_ID")
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
                    deviceType: "phone"
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
                deviceId: AppConfig().deviceID
            )
        } catch {
            // Health is optional — the rest of EV stays up if the owner declines.
        }
    }
}
