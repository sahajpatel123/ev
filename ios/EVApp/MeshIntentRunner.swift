import AVFoundation
import AudioToolbox
import CoreLocation
import CoreMotion
import EVClient
import HealthKit
import UIKit
import UserNotifications

/// The iPhone half of the Evie Mesh: polls the Follow-Me intent
/// bus for intents addressed to THIS device (or broadcast to
/// everyone) and executes them locally, then acks with a receipt.
///
/// Execution rules:
/// - converge.beacon   heavy haptics + chime + banner
/// - photo.capture     capture with this phone's camera, save
///                     with provenance, ack with the filename
/// - shortcut.run      open the named Shortcut (AppIntents)
/// - sensor.read       barometer / altitude (CMAltimeter),
///                     battery (UIDevice), steps (HealthKit),
///                     GPS (one-shot, only if authorized),
///                     flashlight (torch, R2)
/// - nudge.escalate    local notification
/// - clipboard.transform  deterministic tidy of the pasteboard
/// - conversation.migrate  honest handoff record
/// - mac.verb          fails honestly — a phone cannot run Mac verbs
///
/// Every outcome acks DONE or FAILED with a note; nothing is
/// silently dropped and nothing is fabricated.
@MainActor
final class MeshIntentRunner: ObservableObject {
    static let shared = MeshIntentRunner()

    @Published var lastActivity = ""

    private let lock = NSLock()
    private var handled: Set<String> = []
    private var task: Task<Void, Never>?
    private var client: EVAPIClient?
    private var deviceId: String?
    private let location = MeshLocationProvider()

    private init() {}

    func start(client: EVAPIClient, deviceId: String) {
        self.client = client
        self.deviceId = deviceId
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(10))
                await self?.poll()
            }
        }
        Task { await poll() }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    // MARK: - Polling

    func poll() async {
        guard let client else { return }
        do {
            let listing = try await client.listIntents(status: "PENDING")
            for intent in listing.intents where !isHandled(intent.id) {
                await handle(intent, client: client)
            }
        } catch {
            return
        }
    }

    private func isHandled(_ id: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return handled.contains(id)
    }

    private func markHandled(_ id: String) {
        lock.lock()
        if handled.count > 500 { handled.removeAll() }
        handled.insert(id)
        lock.unlock()
    }

    private func handle(_ intent: EvieIntent, client: EVAPIClient) async {
        let isBroadcast = intent.target_device_id == nil
        let isTargeted = intent.target_device_id == deviceId
        guard isBroadcast || isTargeted else { return }
        guard !isHandled(intent.id) else { return }
        markHandled(intent.id)

        switch intent.kind {
        case "converge.beacon":
            converge(reason: stringArg(intent.args, "reason"))
            await ack(client, intent.id, status: "DONE", note: "converged on iPhone")
        case "photo.capture":
            await handlePhotoCapture(intent, client: client)
        case "shortcut.run":
            await handleShortcutRun(intent, client: client)
        case "sensor.read":
            await handleSensorRead(intent, client: client)
        case "nudge.escalate":
            await handleNudge(intent, client: client)
        case "clipboard.transform":
            await handleClipboardTransform(intent, client: client)
        case "conversation.migrate":
            await ack(client, intent.id, status: "DONE", note: "handoff recorded")
        case "mac.verb":
            await ack(client, intent.id, status: "FAILED", note: "mac verbs run on the Mac, not a phone")
        default:
            await ack(client, intent.id, status: "FAILED", note: "unhandled kind on iPhone: \(intent.kind)")
        }
    }

    private func stringArg(_ args: [String: AnyCodableValue], _ key: String) -> String {
        guard case .string(let value)? = args[key] else { return "" }
        return value
    }

    private func ack(_ client: EVAPIClient, _ id: String, status: String, note: String) async {
        do {
            _ = try await client.ackIntent(id: id, status: status, note: note)
            lastActivity = "\(status): \(note)"
        } catch {
            lastActivity = "ack failed: \(note)"
        }
    }

    // MARK: - Vertices

    private func converge(reason: String) {
        let generator = UIImpactFeedbackGenerator(style: .heavy)
        generator.impactOccurred()
        let second = UIImpactFeedbackGenerator(style: .medium)
        second.impactOccurred()
        // System sound 1016 (SMS received) — offline, no assets.
        AudioServicesPlaySystemSound(1016)
        let center = UNUserNotificationCenter.current()
        let content = UNMutableNotificationContent()
        content.title = "Evie Converge"
        content.body = reason.isEmpty ? "Find me" : reason
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: "mesh-converge-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        center.add(request)
    }

    private func handlePhotoCapture(_ intent: EvieIntent, client: EVAPIClient) async {
        do {
            let manager = CameraManager.shared
            guard await manager.requestAccess() else {
                try await ack(client, intent.id, status: "FAILED", note: "camera permission denied")
                return
            }
            let frame = try await manager.captureFrame(forSave: true)
            let saved = manager.savePhoto(frame.jpeg)
            let requester = stringArg(intent.args, "requester_device_id")
            try await ack(
                client, intent.id, status: "DONE",
                note: "captured by iPhone: \(saved.filename) (\(frame.width)x\(frame.height)), asked by \(requester)"
            )
        } catch {
            try? await ack(client, intent.id, status: "FAILED", note: "capture failed: \(error.localizedDescription)")
        }
    }

    private func handleShortcutRun(_ intent: EvieIntent, client: EVAPIClient) async {
        guard case .string(let name)? = intent.args["shortcut"] else {
            try? await ack(client, intent.id, status: "FAILED", note: "shortcut.run without a name")
            return
        }
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? name
        guard let url = URL(string: "shortcuts://run-shortcut?name=\(encoded)"),
              UIApplication.shared.canOpenURL(url)
        else {
            try? await ack(client, intent.id, status: "FAILED", note: "Shortcuts app unavailable")
            return
        }
        await UIApplication.shared.open(url)
        try? await ack(client, intent.id, status: "DONE", note: "opened shortcut: \(name)")
    }

    private func handleSensorRead(_ intent: EvieIntent, client: EVAPIClient) async {
        guard case .string(let sensor)? = intent.args["sensor"] else {
            try? await ack(client, intent.id, status: "FAILED", note: "sensor.read without a sensor")
            return
        }
        switch sensor {
        case "barometer", "altitude":
            await readBarometer(client, intent.id, sensor: sensor)
        case "battery":
            await readBattery(client, intent.id)
        case "steps":
            await readSteps(client, intent.id)
        case "gps":
            await readLocation(client, intent.id)
        case "flashlight":
            await toggleFlashlight(client, intent.id, args: intent.args)
        default:
            try? await ack(client, intent.id, status: "FAILED", note: "no \(sensor) sensor on iPhone")
        }
    }

    private func readBarometer(_ client: EVAPIClient, _ id: String, sensor: String) async {
        guard CMAltimeter.isRelativeAltitudeAvailable() else {
            try? await ack(client, id, status: "FAILED", note: "no barometer on this iPhone")
            return
        }
        do {
            let sample = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<(Double, Double), Error>) in
                let altimeter = CMAltimeter()
                altimeter.startRelativeAltitudeUpdates(to: .main) { data, error in
                    guard let data, error == nil else {
                        continuation.resume(throwing: error ?? CocoaError(.featureUnsupported))
                        return
                    }
                    continuation.resume(returning: (
                        data.pressure.doubleValue,
                        data.relativeAltitude.doubleValue
                    ))
                    altimeter.stopRelativeAltitudeUpdates()
                }
            }
            let note = sensor == "altitude"
                ? "altitude \(sample.1) m, pressure \(sample.0) kPa"
                : "pressure \(sample.0) kPa"
            try await ack(client, id, status: "DONE", note: note)
        } catch {
            try? await ack(client, id, status: "FAILED", note: "barometer read failed")
        }
    }

    private func readBattery(_ client: EVAPIClient, _ id: String) async {
        UIDevice.current.isBatteryMonitoringEnabled = true
        let level = UIDevice.current.batteryLevel
        guard level >= 0 else {
            try? await ack(client, id, status: "FAILED", note: "battery unreadable")
            return
        }
        let state = UIDevice.current.batteryState == .charging ? ", charging" : ""
        try? await ack(client, id, status: "DONE", note: "battery \(Int(level * 100))%\(state)")
    }

    private func readSteps(_ client: EVAPIClient, _ id: String) async {
        do {
            let metrics = try await HealthKitManager.shared.latestMetrics()
            let steps = metrics.metrics.first { $0.key.lowercased().contains("step") }?.value
            guard let steps else {
                try await ack(client, id, status: "FAILED", note: "steps not authorized or unavailable")
                return
            }
            try await ack(client, id, status: "DONE", note: "steps \(Int(steps)) today")
        } catch {
            try? await ack(client, id, status: "FAILED", note: "steps unavailable: \(error.localizedDescription)")
        }
    }

    private func readLocation(_ client: EVAPIClient, _ id: String) async {
        let status = CLLocationManager.authorizationStatus()
        guard status == .authorizedWhenInUse || status == .authorizedAlways else {
            try? await ack(client, id, status: "FAILED", note: "location permission required (R2 consent)")
            return
        }
        do {
            let location = try await location.requestOnce()
            try await ack(
                client, id, status: "DONE",
                note: "location \(location.latitude), \(location.longitude)"
            )
        } catch {
            try? await ack(client, id, status: "FAILED", note: "location read failed")
        }
    }

    private func toggleFlashlight(_ client: EVAPIClient, _ id: String, args: [String: AnyCodableValue]) async {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              device.hasTorch
        else {
            try? await ack(client, id, status: "FAILED", note: "no torch on this iPhone")
            return
        }
        let on = stringArg(args, "state") != "off"
        do {
            try device.lockForConfiguration()
            device.torchMode = on ? .on : .off
            device.unlockForConfiguration()
            try await ack(client, id, status: "DONE", note: "flashlight \(on ? "on" : "off")")
        } catch {
            try? await ack(client, id, status: "FAILED", note: "torch control failed")
        }
    }

    private func handleNudge(_ intent: EvieIntent, client: EVAPIClient) async {
        let title = stringArg(intent.args, "title")
        let body = stringArg(intent.args, "body")
        let content = UNMutableNotificationContent()
        content.title = title.isEmpty ? "Evie" : title
        content.body = body.isEmpty ? "Attention requested" : body
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: "mesh-nudge-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        do {
            try await UNUserNotificationCenter.current().add(request)
            try await ack(client, intent.id, status: "DONE", note: "nudge shown on iPhone")
        } catch {
            try? await ack(client, intent.id, status: "FAILED", note: "nudge notification failed")
        }
    }

    private func handleClipboardTransform(_ intent: EvieIntent, client: EVAPIClient) async {
        guard case .string(let transform)? = intent.args["transform"],
              case .string(let text)? = intent.args["text"]
        else {
            try? await ack(client, intent.id, status: "FAILED", note: "clipboard.transform missing payload")
            return
        }
        switch transform {
        case "tidy":
            let tidy = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            UIPasteboard.general.string = tidy
            try? await ack(client, intent.id, status: "DONE", note: "tidy applied on iPhone: \(tidy.prefix(80))")
        case "speak":
            let generator = UIImpactFeedbackGenerator(style: .light)
            generator.impactOccurred()
            try? await ack(client, intent.id, status: "DONE", note: "speak handled by the asking device")
        default:
            // translate/summarize need the reasoning API; the
            // phone does not fake them.
            try? await ack(client, intent.id, status: "FAILED", note: "\(transform) not available on iPhone")
        }
    }
}

/// One-shot location for sensor.read gps — the only honest way
/// to answer "where is this phone" without a persistent tracker.
final class MeshLocationProvider: NSObject, CLLocationManagerDelegate, @unchecked Sendable {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocationCoordinate2D, Error>?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func requestOnce() async throws -> CLLocationCoordinate2D {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            self.manager.requestLocation()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        continuation?.resume(returning: location.coordinate)
        continuation = nil
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        continuation?.resume(throwing: error)
        continuation = nil
    }
}
