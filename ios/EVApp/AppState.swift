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
}
