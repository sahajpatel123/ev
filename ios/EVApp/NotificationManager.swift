import Foundation
import UserNotifications

/// APNs registration and local notification handling.
///
/// The backend push path is intentionally inert until Agent 14 lands the
/// device-token endpoint; the upload below is the exact call that endpoint
/// will receive, and failures are logged silently so the app never depends
/// on push to work.
@MainActor
final class NotificationManager {
    static let shared = NotificationManager()

    private init() {}

    func requestAuthorization() async -> Bool {
        let center = UNUserNotificationCenter.current()
        do {
            return try await center.requestAuthorization(options: [.alert, .sound, .badge])
        } catch {
            return false
        }
    }

    func upload(deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        UserDefaults.standard.set(hex, forKey: "ev.apnsToken")

        let config = AppConfig()
        let url = config.baseURL
            .appendingPathComponent("v1/devices/\(config.deviceID)/push-token")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(config.apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Any] = [
            "token": hex,
            "platform": "apns",
            "bundle_id": Bundle.main.bundleIdentifier ?? "",
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        Task {
            do {
                _ = try await URLSession.shared.data(for: request)
            } catch {
                // Inert until Agent 14 adds the endpoint; never fail the app.
            }
        }
    }
}
