// EV ambient helper: coarse-only location + text-first screen context.
//
// Design rules (FLEET_LAW #8/#10):
//  * The default JSON output NEVER contains exact coordinates, raw pixels,
//    or audio.  Location is classified into named places ("home"/"work") or
//    "elsewhere" from a user-managed places file; screen output is app name,
//    window title, URL, category and idle time only.
//  * Permission state (Location TCC, Accessibility, Screen Recording) is
//    surfaced so the collector can degrade honestly instead of guessing.
//  * The helper is a thin OS probe; the Python collector owns retention,
//    privacy levels and ingestion.

import AppKit
import ApplicationServices
import CoreGraphics
import CoreLocation
import Foundation

// MARK: - JSON output

func jsonLine(_ dict: [String: Any]) {
    guard
        let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
        let text = String(data: data, encoding: .utf8)
    else { return }
    print(text)
}

// MARK: - Files

func evFile(named name: String) -> URL {
    let env = ProcessInfo.processInfo.environment["EV_LOCATION_FILE"] ?? ""
    if !env.isEmpty, name == "location.json" {
        return URL(fileURLWithPath: env)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".ev")
        .appendingPathComponent(name)
}

func placesFileURL() -> URL {
    let env = ProcessInfo.processInfo.environment["EV_LOCATION_PLACES_FILE"] ?? ""
    if !env.isEmpty { return URL(fileURLWithPath: env) }
    return evFile(named: "location-places.json")
}

func readPlaces() -> [String: [String: Double]] {
    let url = placesFileURL()
    guard
        let data = try? Data(contentsOf: url),
        let object = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Double]]
    else { return [:] }
    return object
}

func haversineMeters(lat1: Double, lon1: Double, lat2: Double, lon2: Double) -> Double {
    let r = 6_371_000.0
    let dLat = (lat2 - lat1) * .pi / 180.0
    let dLon = (lon2 - lon1) * .pi / 180.0
    let a =
        sin(dLat / 2) * sin(dLat / 2)
        + cos(lat1 * .pi / 180.0) * cos(lat2 * .pi / 180.0)
        * sin(dLon / 2) * sin(dLon / 2)
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))
}

/// Classify a fix against user-defined named places.  Never returns coordinates.
func classifyPlace(latitude: Double, longitude: Double) -> (place: String?, presence: String) {
    let places = readPlaces()
    if places.isEmpty {
        return (nil, "elsewhere")
    }
    var best: (name: String, distance: Double)?
    for (name, spec) in places {
        guard let lat = spec["latitude"], let lon = spec["longitude"] else { continue }
        let radius = spec["radius_m"] ?? 300.0
        let distance = haversineMeters(lat1: latitude, lon1: longitude, lat2: lat, lon2: lon)
        if distance <= radius, best == nil || distance < best!.distance {
            best = (name, distance)
        }
    }
    if let best {
        return (best.name, best.name)
    }
    return (nil, "elsewhere")
}

// MARK: - App category

let CATEGORY_BY_BUNDLE: [String: String] = [
    "com.apple.dt.Xcode": "developer",
    "com.microsoft.VSCode": "developer",
    "com.jetbrains.intellij": "developer",
    "com.sublimetext.4": "developer",
    "com.github.atom": "developer",
    "com.google.Chrome": "browser",
    "com.apple.Safari": "browser",
    "org.mozilla.firefox": "browser",
    "com.microsoft.edgemac": "browser",
    "com.apple.mail": "communication",
    "com.apple.MobileSMS": "communication",
    "com.apple.FaceTime": "communication",
    "us.zoom.xos": "meeting",
    "com.microsoft.teams": "meeting",
    "com.google.Chrome.app.XXX": "meeting",
    "com.apple.Notes": "productivity",
    "com.apple.Pages": "productivity",
    "com.microsoft.Word": "productivity",
    "com.microsoft.Excel": "productivity",
    "com.apple.Keynote": "presentation",
    "com.microsoft.Powerpoint": "presentation",
    "com.apple.Music": "entertainment",
    "com.apple.TV": "entertainment",
    "com.apple.Photos": "media",
    "com.adobe.Photoshop": "design",
    "com.figma.Desktop": "design",
    "com.slack.Slack": "communication",
]

func category(forBundle bundleID: String) -> String? {
    if bundleID.hasPrefix("com.google.Chrome") || bundleID.hasPrefix("com.apple.Safari") {
        return "browser"
    }
    return CATEGORY_BY_BUNDLE[bundleID]
}

// MARK: - Screen snapshot

func windowInfo(ownerPID: pid_t) -> [[String: Any]] {
    guard let raw = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements],
        kCGNullWindowID
    ) as? [[String: Any]] else { return [] }
    return raw.filter { window in
        guard
            let pid = window["kCGWindowOwnerPID"] as? Int,
            pid == Int(ownerPID),
            let layer = window["kCGWindowLayer"] as? Int,
            layer == 0
        else { return false }
        return true
    }
}

func windowArea(_ window: [String: Any]) -> Double {
    guard
        let bounds = window["kCGWindowBounds"] as? [String: Any],
        let width = bounds["Width"] as? Double,
        let height = bounds["Height"] as? Double
    else { return 0 }
    return width * height
}

func axInfo(pid: pid_t) -> (title: String?, url: String?, document: String?) {
    let app = AXUIElementCreateApplication(pid)
    var focused: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &focused)
    guard error == .success, let focused else { return (nil, nil, nil) }
    let window = focused as! AXUIElement

    func attribute(_ key: CFString, of element: AXUIElement) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, key, &value) == .success, let value else {
            return nil
        }
        return value as? String
    }

    return (
        attribute(kAXTitleAttribute as CFString, of: window),
        attribute(kAXURLAttribute as CFString, of: window),
        attribute(kAXDocumentAttribute as CFString, of: window)
    )
}

func screenSnapshot() -> [String: Any] {
    var out: [String: Any] = [:]
    out["accessibility_granted"] = AXIsProcessTrusted()
    out["screen_recording_granted"] = CGPreflightScreenCaptureAccess()
    // kCGAnyInputEventType == ~0: any keyboard/mouse/tablet event resets idle.
    if let anyInput = CGEventType(rawValue: 0xFFFF_FFFF) {
        out["idle_seconds"] = CGEventSource.secondsSinceLastEventType(
            .combinedSessionState,
            eventType: anyInput
        )
    }

    guard let front = NSWorkspace.shared.frontmostApplication else { return out }
    out["app_name"] = front.localizedName ?? ""
    if let bundle = front.bundleIdentifier {
        out["bundle_id"] = bundle
        if let category = category(forBundle: bundle) {
            out["category"] = category
        }
    }
    let pid = front.processIdentifier
    let windows = windowInfo(ownerPID: pid)
    if let largest = windows.max(by: { windowArea($0) < windowArea($1) }),
       let title = largest["kCGWindowName"] as? String,
       !title.isEmpty {
        out["window_title"] = title
    }
    let accessibility = axInfo(pid: pid)
    if out["window_title"] == nil, let title = accessibility.title, !title.isEmpty {
        out["window_title"] = title
    }
    if let url = accessibility.url, !url.isEmpty {
        out["url"] = url
    }
    if let document = accessibility.document, !document.isEmpty {
        out["document"] = document
    }
    return out
}

// MARK: - Location

enum LocationProbeError: LocalizedError {
    case denied
    case unavailable

    var errorDescription: String? {
        switch self {
        case .denied: return "location permission denied or restricted"
        case .unavailable: return "no location fix available"
        }
    }
}

func statusString(_ status: CLAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorizedAlways: return "authorizedAlways"
    case .authorizedWhenInUse: return "authorizedWhenInUse"
    @unknown default: return "unknown"
    }
}

func currentAuthorizationStatus() -> CLAuthorizationStatus {
    CLLocationManager().authorizationStatus
}

final class LocationProbe: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var promptIfNeeded = false
    var onLocation: ((CLLocation) -> Void)?
    var onError: ((Error) -> Void)?

    func start(promptIfNeeded: Bool) {
        self.promptIfNeeded = promptIfNeeded
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        let status = currentAuthorizationStatus()
        switch status {
        case .authorizedAlways, .authorizedWhenInUse:
            manager.requestLocation()
        case .notDetermined:
            if promptIfNeeded {
                manager.requestWhenInUseAuthorization()
            } else {
                onError?(LocationProbeError.unavailable)
            }
        default:
            onError?(LocationProbeError.denied)
        }
    }

    func startSignificantChanges(promptIfNeeded: Bool) {
        self.promptIfNeeded = promptIfNeeded
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        let status = currentAuthorizationStatus()
        switch status {
        case .authorizedAlways, .authorizedWhenInUse:
            manager.startMonitoringSignificantLocationChanges()
        case .notDetermined:
            if promptIfNeeded {
                manager.requestWhenInUseAuthorization()
            } else {
                onError?(LocationProbeError.unavailable)
            }
        default:
            onError?(LocationProbeError.denied)
        }
    }

    func stop() {
        manager.stopUpdatingLocation()
        manager.stopMonitoringSignificantLocationChanges()
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        switch status {
        case .authorizedAlways, .authorizedWhenInUse:
            manager.requestLocation()
        case .denied, .restricted:
            onError?(LocationProbeError.denied)
        default:
            break
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        if let location = locations.last {
            onLocation?(location)
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        onError?(error)
    }
}

func locationSnapshot(location: CLLocation?) -> [String: Any] {
    var out: [String: Any] = [:]
    out["authorization_status"] = statusString(currentAuthorizationStatus())
    if let location {
        out["location_available"] = true
        let classified = classifyPlace(
            latitude: location.coordinate.latitude,
            longitude: location.coordinate.longitude
        )
        if let place = classified.place {
            out["place"] = place
        }
        out["presence"] = classified.presence
        out["timestamp"] = ISO8601DateFormatter().string(from: location.timestamp)
    } else {
        out["location_available"] = false
        out["presence"] = "unknown"
    }
    return out
}

func writeLocationFile(_ snapshot: [String: Any]) {
    let url = evFile(named: "location.json")
    try? FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    guard let data = try? JSONSerialization.data(
        withJSONObject: snapshot,
        options: [.sortedKeys, .prettyPrinted]
    ) else { return }
    try? data.write(to: url)
}

func runOneShot(timeout: TimeInterval, promptIfNeeded: Bool) -> (location: CLLocation?, error: String?) {
    let probe = LocationProbe()
    var location: CLLocation?
    var errorText: String?
    let semaphore = DispatchSemaphore(value: 0)
    probe.onLocation = { location = $0; semaphore.signal() }
    probe.onError = { errorText = $0.localizedDescription; semaphore.signal() }
    probe.start(promptIfNeeded: promptIfNeeded)
    _ = semaphore.wait(timeout: .now() + timeout)
    probe.stop()
    return (location, errorText)
}

func runMonitor(seconds: TimeInterval, promptIfNeeded: Bool) {
    let probe = LocationProbe()
    probe.onLocation = { location in
        let snapshot = locationSnapshot(location: location)
        jsonLine(snapshot)
        writeLocationFile(snapshot)
    }
    probe.onError = { error in
        jsonLine([
            "authorization_status": statusString(currentAuthorizationStatus()),
            "error": error.localizedDescription,
        ])
    }
    probe.startSignificantChanges(promptIfNeeded: promptIfNeeded)
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
    }
    probe.stop()
}

// MARK: - CLI

let arguments = CommandLine.arguments
if arguments.contains("--screen") {
    jsonLine(screenSnapshot())
} else if arguments.contains("--location") {
    let prompt = arguments.contains("--prompt") || !arguments.contains("--no-prompt")
    let (location, error) = runOneShot(timeout: 8, promptIfNeeded: prompt)
    var snapshot = locationSnapshot(location: location)
    if let error {
        snapshot["error"] = error
    }
    jsonLine(snapshot)
} else if arguments.contains("--monitor") {
    var seconds: TimeInterval = 60
    if let index = arguments.firstIndex(of: "--seconds"),
       index + 1 < arguments.count,
       let value = TimeInterval(arguments[index + 1]) {
        seconds = value
    }
    let prompt = arguments.contains("--prompt") || !arguments.contains("--no-prompt")
    runMonitor(seconds: seconds, promptIfNeeded: prompt)
} else {
    print(
        """
        usage: ambient_helper --screen | --location [--no-prompt] | --monitor [--seconds N] [--no-prompt]
        """
    )
}
