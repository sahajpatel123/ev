import Foundation
@preconcurrency import UserNotifications

/// Single notification delivery path.
///
/// Agent 14's backend invokes the ``EVNotificationHelper`` binary, which opens
/// `ev://notification?...`. This bridge (running inside the bundled EV.app)
/// is the only component that calls UNUserNotificationCenter, so there is
/// exactly one native delivery path and it carries a real bundle identity.
@MainActor
final class NotificationBridge {
    static let shared = NotificationBridge()

    private init() {}

    func handle(url: URL) {
        guard url.scheme?.lowercased() == "ev" else { return }
        switch url.host?.lowercased() {
        case "notification":
            let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let value = { name in items.first(where: { $0.name == name })?.value }
            post(
                title: value("title") ?? "EV",
                body: value("body") ?? "",
                identifier: value("id") ?? UUID().uuidString
            )
        case "notify-check":
            break
        default:
            break
        }
    }

    func post(title: String, body: String, identifier: String) {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral:
                let content = UNMutableNotificationContent()
                content.title = title
                content.body = body
                content.sound = .default
                let request = UNNotificationRequest(
                    identifier: identifier,
                    content: content,
                    trigger: nil
                )
                center.add(request)
            case .notDetermined:
                center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
                    if granted {
                        Task { @MainActor in
                            self.post(title: title, body: body, identifier: identifier)
                        }
                    }
                }
            case .denied:
                break
            @unknown default:
                break
            }
        }
    }
}
