import Foundation
import MapKit
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
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        // Python urlencode historically used "+"; URLComponents leaves that
        // as a literal plus, which painted "No+update+from+him" on the HUD.
        let value = { (name: String) -> String? in
            guard let raw = items.first(where: { $0.name == name })?.value else {
                return nil
            }
            return raw.replacingOccurrences(of: "+", with: " ")
        }
        switch url.host?.lowercased() {
        case "present":
            let kind = PresenceKind(rawValue: value("kind") ?? "card") ?? .card
            let size = PresenceSize(rawValue: value("size") ?? "") ?? kind.defaultSize
            let timeType = PresenceTimeType(rawValue: value("time") ?? "") ?? kind.defaultTime
            let placement = PresencePlacement(rawValue: value("place") ?? "") ?? kind.defaultPlacement
            let items = (value("items") ?? "")
                .split(whereSeparator: { $0 == "|" || $0 == "\n" })
                .map(String.init)
            let questions = (value("questions") ?? "")
                .split(whereSeparator: { $0 == "|" || $0 == "\n" })
                .map(String.init)
            let lat = value("lat").flatMap(Double.init)
            let lon = value("lon").flatMap(Double.init)
            let destLat = value("dest_lat").flatMap(Double.init)
            let destLon = value("dest_lon").flatMap(Double.init)
            let ttlMs = value("ttl").flatMap(Double.init)
            let windowId = value("id") ?? "hud-\(UUID().uuidString)"
            let lookout = value("lookout") == "1" || kind.isLookoutDefault
            PresenceController.shared.show(PresenceContent(
                id: windowId,
                title: value("title") ?? "EVIE",
                message: value("body") ?? "",
                kind: kind,
                size: size,
                timeType: timeType,
                placement: placement,
                items: items,
                questions: questions,
                response: value("response"),
                recommendation: value("recommendation"),
                source: value("source"),
                lookout: lookout,
                ttl: ttlMs.map { $0 / 1000.0 },
                layout: value("layout"),
                driftX: value("dx").flatMap(Double.init).map { CGFloat($0) },
                driftY: value("dy").flatMap(Double.init).map { CGFloat($0) },
                tilt: value("tilt").flatMap(Double.init),
                origin: (lat != nil && lon != nil)
                    ? CLLocationCoordinate2D(latitude: lat!, longitude: lon!)
                    : nil,
                destination: (destLat != nil && destLon != nil)
                    ? CLLocationCoordinate2D(latitude: destLat!, longitude: destLon!)
                    : nil
            ))
        case "dismiss":
            PresenceController.shared.hide(id: value("id"))
        case "dismiss-all":
            PresenceController.shared.hideAll()
        case "notification":
            post(
                title: value("title") ?? "EVIE",
                body: value("body") ?? "",
                identifier: value("id") ?? UUID().uuidString
            )
        case "notify-check":
            break
        case "permissions-request":
            // URL-triggered permission prompt, so the running (foreground)
            // app can fire TCC consent dialogs reliably — the CLI process
            // cannot present menu-bar accessory prompts. `kind` selects a
            // single permission; omit it to run the pending sweep.
            let kind = value("kind").flatMap(PermissionKind.init(rawValue:))
            Task {
                if let kind {
                    _ = await PermissionCenter.request(kind)
                } else {
                    _ = await PermissionCenter.requestPending()
                }
            }
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
