import EVClient
import Foundation

@MainActor
final class AppState: ObservableObject {
    let client: EVAPIClient
    let queue: OfflineCaptureQueue

    init() {
        let config = AppConfig()
        client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)

        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("EVQueue", isDirectory: true)
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("EVQueue", isDirectory: true)
        try? FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        queue = OfflineCaptureQueue(store: FileCaptureQueueStore(directory: documents))
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
        _ = try? await client.bootstrapDevice(id: registryId)
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
