import Foundation
#if canImport(CoreLocation)
import CoreLocation
#endif
#if canImport(UserNotifications)
import UserNotifications
#endif

// Cycle — iPhone-only, backward compat: additive geofence monitor for location reminders.
// Body-not-brain: no model calls, no DeviceAuth token use, no continuous tracking.
// Region monitoring only (CLCircularRegion); minimal region state persisted in UserDefaults.
// Auth stays in CapabilityBroker (execute/bind_session); this file only executes + reports.
// Wiring hook: CapabilityBroker.handle can call GeofenceMonitor.shared methods; no broker edits here.

/// Minimal persisted region state. Codable so it round-trips through UserDefaults as JSON.
struct EvieGeofence: Codable, Sendable, Equatable {
    var id: String
    var label: String
    var latitude: Double
    var longitude: Double
    var radiusMeters: Double
    /// Gateway action id that created this region, if any. Kept for completion reporting only.
    var actionID: String?
    var createdAtISO: String
}

#if os(iOS)
/// Region-event monitor. Never calls startUpdatingLocation — no continuous tracking.
@MainActor
final class GeofenceMonitor: NSObject {
    static let shared = GeofenceMonitor()

    private static let storeKey = "evie.geofences"
    private static let maxRegions = 20 // iOS allows ~20 monitored regions per app.
    private static let minRadius = 50.0
    private static let maxRadius = 2000.0

    private lazy var manager: CLLocationManager = {
        let manager = CLLocationManager()
        manager.delegate = delegateBridge
        return manager
    }()

    private let delegateBridge = GeofenceDelegateBridge()

    private override init() {
        super.init()
        delegateBridge.onEnter = { [weak self] identifier in
            self?.handleEntry(identifier: identifier)
        }
        manager.delegate = delegateBridge
    }

    // MARK: - Broker hook surface (payloads use BrokerOutcome vocabulary)

    /// Adds (or replaces) a reminder region. Returns a broker-style payload dict.
    func addRegion(
        id: String,
        label: String,
        latitude: Double,
        longitude: Double,
        radiusMeters: Double,
        actionID: String? = nil
    ) -> [String: Any] {
        guard (-90...90).contains(latitude), (-180...180).contains(longitude) else {
            return ["ok": false, "result": "FAILED", "failure": "EXECUTION_FAILED"]
        }
        guard CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) else {
            return ["ok": false, "result": "FAILED", "failure": "ACTION_UNAVAILABLE"]
        }
        var regions = loadRegions()
        let clamped = min(max(radiusMeters, Self.minRadius), Self.maxRadius)
        let fence = EvieGeofence(
            id: id,
            label: label,
            latitude: latitude,
            longitude: longitude,
            radiusMeters: clamped,
            actionID: actionID,
            createdAtISO: ISO8601DateFormatter().string(from: Date())
        )
        regions.removeAll { $0.id == id }
        regions.append(fence)
        while regions.count > Self.maxRegions { regions.removeFirst() }
        saveRegions(regions)

        let status = manager.authorizationStatus
        guard status == .authorizedWhenInUse || status == .authorizedAlways else {
            manager.requestWhenInUseAuthorization()
            return [
                "ok": false,
                "result": "PERMISSION_REQUIRED",
                "failure": "PERMISSION_REQUIRED",
                "region": fence.payload,
            ]
        }
        startMonitoring(fence)
        return ["ok": true, "result": "CREATED", "executed": true, "verified": false, "region": fence.payload]
    }

    /// Stops monitoring and drops persisted state for one region.
    func removeRegion(id: String) -> [String: Any] {
        for region in manager.monitoredRegions where region.identifier == id {
            manager.stopMonitoring(for: region)
        }
        var regions = loadRegions()
        regions.removeAll { $0.id == id }
        saveRegions(regions)
        return ["ok": true, "result": "EXECUTED", "executed": true, "verified": false]
    }

    /// Re-registers persisted regions after launch. Call from app foreground; no-op without permission.
    func startMonitoringAll() {
        let status = manager.authorizationStatus
        guard status == .authorizedWhenInUse || status == .authorizedAlways else { return }
        for fence in loadRegions() {
            startMonitoring(fence)
        }
    }

    /// Read-only snapshot for permission evidence / broker replies.
    func snapshotPayload() -> [String: Any] {
        [
            "ok": true,
            "permission": Self.permissionLabel(status: manager.authorizationStatus),
            "monitoring_available": CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self),
            "regions": loadRegions().map { $0.payload },
            "continuous_tracking": false,
            "sent_to_model": false,
        ]
    }

    static func permissionLabel(status: CLAuthorizationStatus) -> String {
        switch status {
        case .authorizedAlways, .authorizedWhenInUse: return "granted"
        case .denied, .restricted: return "denied"
        default: return "undetermined"
        }
    }

    // MARK: - Private

    private func startMonitoring(_ fence: EvieGeofence) {
        let center = CLLocationCoordinate2D(latitude: fence.latitude, longitude: fence.longitude)
        let region = CLCircularRegion(
            center: center,
            radius: fence.radiusMeters,
            identifier: fence.id
        )
        region.notifyOnEntry = true
        region.notifyOnExit = false
        manager.startMonitoring(for: region)
    }

    private func handleEntry(identifier: String) {
        let regions = loadRegions()
        let label = regions.first { $0.id == identifier }?.label ?? "Reminder"
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date()),
            forKey: "evie.geofence.last_entry.\(identifier)"
        )
        let content = UNMutableNotificationContent()
        content.title = label
        content.body = label
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: "evie-geofence-\(identifier)",
            content: content,
            trigger: nil // Immediate: the region event IS the trigger.
        )
        UNUserNotificationCenter.current().add(request)
    }

    private func loadRegions() -> [EvieGeofence] {
        guard let data = UserDefaults.standard.data(forKey: Self.storeKey) else { return [] }
        return (try? JSONDecoder().decode([EvieGeofence].self, from: data)) ?? []
    }

    private func saveRegions(_ regions: [EvieGeofence]) {
        let data = try? JSONEncoder().encode(regions)
        UserDefaults.standard.set(data, forKey: Self.storeKey)
    }
}

/// NSObject delegate carrier: CLLocationManager.delegate must be an NSObject; the
/// @MainActor GeofenceMonitor cannot satisfy the nonisolated delegate requirement directly.
private final class GeofenceDelegateBridge: NSObject, CLLocationManagerDelegate {
    var onEnter: ((String) -> Void)?

    func locationManager(_ manager: CLLocationManager, didEnterRegion region: CLRegion) {
        onEnter?(region.identifier)
    }
}

private extension EvieGeofence {
    var payload: [String: Any] {
        var dict: [String: Any] = [
            "id": id,
            "label": label,
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": radiusMeters,
            "created_at": createdAtISO,
        ]
        if let actionID { dict["action_id"] = actionID }
        return dict
    }
}
#else
/// Non-iOS fallback so intent stays visible outside the Xcode target.
enum GeofenceMonitor {
    static func snapshotPayload() -> [String: Any] {
        ["ok": false, "result": "FAILED", "failure": "ACTION_UNAVAILABLE"]
    }
}
#endif
