import CoreBluetooth
import Foundation
#if canImport(UIKit)
import UIKit
#endif

// Evie Mesh — the ambient cross-device layer, shared by iOS EVApp
// and macOS EV.app.
//
// Two halves:
// 1. BLE constellation: every device advertises the EV mesh service
//    and observes its peers' signal strength. Observations flow to
//    the backend, which learns WHO is physically near WHOM — the
//    evidence the Follow-Me resolvers bias on.
// 2. Mesh intent client: typed endpoints for converge beacons,
//    remote photo capture, shortcut bridging, sensor reads, nudge
//    escalation, clipboard transforms, conversation migration, and
//    approval-gated Mac verbs.
//
// Honest limits, stated not hidden: iOS background BLE is restricted
// to service-UUID advertising and (with the bluetooth-central
// background mode) filtered scanning; the always-running Mac menubar
// app is the reliable mesh anchor. A device that is not advertising
// is reported as unknown — never guessed.

#if canImport(CoreBluetooth)

// MARK: - Zones

public enum EvieMeshZone: String, Sendable {
    case immediate
    case near
    case far
    case edge
    case unknown

    public init(rssi: Double?) {
        guard let rssi else { self = .unknown; return }
        if rssi >= -55 { self = .immediate }
        else if rssi >= -70 { self = .near }
        else if rssi >= -85 { self = .far }
        else { self = .edge }
    }

    public var rank: Int {
        switch self {
        case .immediate: 0
        case .near: 1
        case .far: 2
        case .edge: 3
        case .unknown: 4
        }
    }
}

public let EV_MESH_SERVICE_UUID = "9F5E2C7A-4B1D-4E63-8A0F-2C7D1B3E5A90"

// MARK: - Sighting

public struct EvieMeshSighting: Equatable, Sendable {
    public var subjectDeviceId: String
    public var bleIdentifier: String
    public var rssi: Double?
    public var zone: EvieMeshZone
    public var batteryPercent: Double?
    public var capabilities: [String]
    public var observedAt: Date

    public init(
        subjectDeviceId: String,
        bleIdentifier: String,
        rssi: Double?,
        batteryPercent: Double? = nil,
        capabilities: [String] = []
    ) {
        self.subjectDeviceId = subjectDeviceId
        self.bleIdentifier = bleIdentifier
        self.rssi = rssi
        self.zone = EvieMeshZone(rssi: rssi)
        self.batteryPercent = batteryPercent
        self.capabilities = capabilities
        self.observedAt = Date()
    }
}

// MARK: - Roster

/// Maps the short id carried in an advertised local name
/// ("ev-<short>") back to the owner's device id. Learned once
/// per pair of devices when both are foreground; persisted.
public actor EvieMeshRoster {
    public static let shared = EvieMeshRoster()
    private let defaults = UserDefaults(suiteName: "ev.mesh") ?? .standard
    private let key = "roster-v1"

    public init() {}

    public func learn(shortId: String, deviceId: String) {
        guard !shortId.isEmpty, !deviceId.isEmpty else { return }
        var map = dictionary()
        map[shortId] = deviceId
        defaults.set(map, forKey: key)
    }

    public func deviceId(forShortId shortId: String) -> String? {
        dictionary()[shortId]
    }

    public func all() -> [String: String] {
        dictionary()
    }

    private func dictionary() -> [String: String] {
        (defaults.dictionary(forKey: key) as? [String: String]) ?? [:]
    }
}

// MARK: - Advertiser

/// Advertises this device into the Evie Mesh over BLE.
///
/// Foreground advertisements carry the local name
/// "ev-<shortDeviceId>" so peers can resolve identity; iOS
/// background advertisements carry only the service UUID, which
/// still yields proximity (RSSI) evidence.
public final class EvieMeshAdvertiser: NSObject, CBPeripheralManagerDelegate, @unchecked Sendable {
    public static let shared = EvieMeshAdvertiser()

    private let lock = NSLock()
    private var manager: CBPeripheralManager?
    private var shortId = ""
    private var lastCapabilities: [String] = []
    private var lastBattery: Double?

    @MainActor
    public static var isAdvertising: Bool {
        get { shared.lock.lock(); defer { shared.lock.unlock() }
              return shared.manager?.isAdvertising == true }
    }

    public func start(deviceId: String, batteryPercent: Double? = nil, capabilities: [String] = []) {
        lock.lock()
        shortId = String(deviceId.prefix(8))
        lastBattery = batteryPercent
        lastCapabilities = capabilities
        if manager == nil {
            manager = CBPeripheralManager(delegate: self, queue: DispatchQueue(label: "com.ev.mesh.advertise"))
        }
        lock.unlock()
    }

    public func stop() {
        lock.lock()
        manager?.stopAdvertising()
        lock.unlock()
    }

    public func updateState(batteryPercent: Double?, capabilities: [String]) {
        lock.lock()
        lastBattery = batteryPercent
        lastCapabilities = capabilities
        lock.unlock()
    }

    nonisolated public func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        guard peripheral.state == .poweredOn else { return }
        advertiseNow()
    }

    private func advertiseNow() {
        lock.lock()
        guard let manager, manager.state == .poweredOn else {
            lock.unlock(); return
        }
        let name = "ev-\(shortId)"
        let service = CBUUID(string: EV_MESH_SERVICE_UUID)
        let payload: [String: Any] = [
            CBAdvertisementDataServiceUUIDsKey: [service],
            CBAdvertisementDataLocalNameKey: name,
        ]
        _ = lastBattery
        _ = lastCapabilities
        manager.startAdvertising(payload)
        lock.unlock()
    }
}

// MARK: - Scanner

/// Scans for Evie Mesh peers and samples their RSSI in bounded
/// cycles (scan 2 s, rest 3 s) — the standard low-power pattern,
/// and the only honest way to keep RSSI fresh without connecting.
public final class EvieMeshScanner: NSObject, CBCentralManagerDelegate, @unchecked Sendable {
    public static let shared = EvieMeshScanner()

    public struct Callback {
        public let onSighting: (EvieMeshSighting) -> Void
        public init(onSighting: @escaping (EvieMeshSighting) -> Void) {
            self.onSighting = onSighting
        }
    }

    private let lock = NSLock()
    private var manager: CBCentralManager?
    private var callbacks: [Callback] = []
    private var cycleTimer: DispatchSourceTimer?
    private var scanning = false
    private var seenInCycle: Set<UUID> = []
    private let cycleQueue = DispatchQueue(label: "com.ev.mesh.scan")

    override public init() {
        super.init()
        manager = CBCentralManager(delegate: self, queue: cycleQueue)
    }

    public func addCallback(_ callback: Callback) {
        lock.lock()
        callbacks.append(callback)
        lock.unlock()
    }

    /// Replace every callback with one — used by the shared store so a
    /// restart (toggle off/on) cannot stack duplicate sightings.
    public func setCallback(_ callback: Callback) {
        lock.lock()
        callbacks = [callback]
        lock.unlock()
    }

    public func start() {
        lock.lock()
        guard cycleTimer == nil else {
            lock.unlock(); return
        }
        let timer = DispatchSource.makeTimerSource(queue: cycleQueue)
        timer.schedule(deadline: .now(), repeating: .seconds(5), leeway: .milliseconds(500))
        timer.setEventHandler { [weak self] in
            self?.cycle()
        }
        timer.resume()
        cycleTimer = timer
        lock.unlock()
        cycle()
    }

    public func stop() {
        lock.lock()
        cycleTimer?.cancel()
        cycleTimer = nil
        manager?.stopScan()
        scanning = false
        lock.unlock()
    }

    private func cycle() {
        lock.lock()
        seenInCycle.removeAll()
        guard let manager, manager.state == .poweredOn else {
            lock.unlock(); return
        }
        let service = CBUUID(string: EV_MESH_SERVICE_UUID)
        manager.scanForPeripherals(
            withServices: [service],
            options: [CBCentralManagerScanOptionAllowDuplicatesKey: true]
        )
        scanning = true
        lock.unlock()
        // Bounded scan window, then rest.
        cycleQueue.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            self?.lock.lock()
            self?.manager?.stopScan()
            self?.lock.unlock()
        }
    }

    nonisolated public func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state != .poweredOn {
            stop()
        }
    }

    nonisolated public func centralManager(
        _ central: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData: [String: Any],
        rssi RSSI: NSNumber
    ) {
        lock.lock()
        let fresh = !seenInCycle.contains(peripheral.identifier)
        if fresh { seenInCycle.insert(peripheral.identifier) }
        let callbacks = self.callbacks
        lock.unlock()
        guard fresh else { return }

        let rssi = RSSI.doubleValue
        // The local name carries the 8-char short id ("ev-<shortId>").
        // The backend resolves short ids to full registry UUIDs, so the
        // sighting reports exactly what BLE can honestly prove; an ad
        // without a name (background iOS) is proximity-only and reports
        // the rotating peripheral UUID.
        var subjectId = peripheral.identifier.uuidString
        if let name = peripheral.name, name.hasPrefix("ev-") {
            subjectId = String(name.dropFirst(3))
        }
        let sighting = EvieMeshSighting(
            subjectDeviceId: subjectId,
            bleIdentifier: peripheral.identifier.uuidString,
            rssi: rssi,
            batteryPercent: nil,
            capabilities: []
        )
        for callback in callbacks {
            callback.onSighting(sighting)
        }
    }
}

// MARK: - Store

/// MainActor-observable mesh state for SwiftUI surfaces.
@MainActor
public final class EvieMeshStore: ObservableObject {
    /// One constellation per process: the app lifecycle owns it, not a
    /// settings sheet (a view-owned store deallocates and silently stops
    /// reporting while the BLE singletons keep scanning).
    public static let shared = EvieMeshStore()

    @Published public var isMeshEnabled = false
    @Published public var sightings: [EvieMeshSighting] = []
    @Published public var lastError: String?

    private let reporterLock = NSLock()
    private var client: EVAPIClient?
    private var deviceId: String?
    private var reportTask: Task<Void, Never>?

    public init() {}

    /// Starts the constellation: advertise + scan + periodic report.
    public func start(client: EVAPIClient, deviceId: String, capabilities: [String]) {
        if isMeshEnabled, self.deviceId == deviceId { return }
        self.client = client
        self.deviceId = deviceId
        EvieMeshAdvertiser.shared.start(deviceId: deviceId, capabilities: capabilities)
        EvieMeshScanner.shared.setCallback(
            EvieMeshScanner.Callback { [weak self] sighting in
                Task { @MainActor in
                    self?.record(sighting)
                }
            }
        )
        EvieMeshScanner.shared.start()
        isMeshEnabled = true
        reportTask?.cancel()
        reportTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                await self?.report()
            }
        }
    }

    public func stop() {
        reportTask?.cancel()
        reportTask = nil
        EvieMeshAdvertiser.shared.stop()
        EvieMeshScanner.shared.stop()
        isMeshEnabled = false
    }

    private func record(_ sighting: EvieMeshSighting) {
        sightings.removeAll { $0.bleIdentifier == sighting.bleIdentifier }
        sightings.insert(sighting, at: 0)
        if sightings.count > 32 { sightings.removeLast(sightings.count - 32) }
    }

    /// Reports fresh observations + own advertisement to the backend.
    public func report() async {
        guard let client, let deviceId else { return }
        let fresh = sightings.prefix(16)
        var observations: [[String: Any]] = []
        for sighting in fresh {
            var entry: [String: Any] = ["subject_device_id": sighting.subjectDeviceId]
            if let rssi = sighting.rssi { entry["rssi"] = rssi }
            if let battery = sighting.batteryPercent { entry["battery_percent"] = battery }
            if !sighting.capabilities.isEmpty { entry["capabilities"] = sighting.capabilities }
            observations.append(entry)
        }
        do {
            if !observations.isEmpty {
                _ = try await client.observeProximity(observations: observations)
            }
            _ = try await client.advertiseMesh(
                batteryPercent: Self.batteryPercent(),
                lowPower: false,
                capabilities: ["mesh"]
            )
        } catch {
            lastError = String(describing: error)
        }
    }

    private static func batteryPercent() -> Double? {
        #if os(iOS)
        UIDevice.current.isBatteryMonitoringEnabled = true
        let level = UIDevice.current.batteryLevel
        return level >= 0 ? Double(level) * 100.0 : nil
        #else
        return nil
        #endif
    }

    public func zone(to subjectDeviceId: String) -> EvieMeshZone {
        sightings.first { $0.subjectDeviceId == subjectDeviceId }?.zone ?? .unknown
    }
}

// MARK: - API client

public struct EvieProximityObservation: Codable, Sendable {
    public let subjectDeviceId: String
    public let rssi: Double?
    public let batteryPercent: Double?
    public let capabilities: [String]?

    public init(subjectDeviceId: String, rssi: Double? = nil, batteryPercent: Double? = nil, capabilities: [String]? = nil) {
        self.subjectDeviceId = subjectDeviceId
        self.rssi = rssi
        self.batteryPercent = batteryPercent
        self.capabilities = capabilities
    }

    enum CodingKeys: String, CodingKey {
        case subjectDeviceId = "subject_device_id"
        case rssi
        case batteryPercent = "battery_percent"
        case capabilities
    }
}

public struct EvieProximityRecord: Codable, Sendable {
    public let observerDeviceId: String?
    public let subjectDeviceId: String
    public let rssi: Double?
    public let zone: String
    public let batteryPercent: Double?
    public let capabilities: [String]?
    public let observedAt: String

    enum CodingKeys: String, CodingKey {
        case observerDeviceId = "observer_device_id"
        case subjectDeviceId = "subject_device_id"
        case rssi, zone
        case batteryPercent = "battery_percent"
        case capabilities
        case observedAt = "observed_at"
    }
}

public struct EvieProximityResponse: Codable, Sendable {
    public let ok: Bool
    public let recorded: [EvieProximityRecord]
    public let count: Int
}

public struct EvieProximityReadout: Codable, Sendable {
    public let ok: Bool
    public let observerDeviceId: String?
    public let proximityRanks: [String: Int]
    public let zones: [String: Int]
    public let serviceUuid: String

    enum CodingKeys: String, CodingKey {
        case ok
        case observerDeviceId = "observer_device_id"
        case proximityRanks = "proximity_ranks"
        case zones
        case serviceUuid = "service_uuid"
    }
}

public struct EvieMeshAdvertisementRecord: Codable, Sendable {
    public let deviceId: String
    public let batteryPercent: Double?
    public let lowPower: Bool
    public let capabilities: [String]?
    public let advertisedAt: String

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case batteryPercent = "battery_percent"
        case lowPower
        case capabilities
        case advertisedAt = "advertised_at"
    }
}

public struct EvieMeshAdvertiseResponse: Codable, Sendable {
    public let ok: Bool
    public let advertisement: EvieMeshAdvertisementRecord
}

public struct EvieMeshIntentResponse: Codable, Sendable {
    public let ok: Bool
    public let intentId: String?
    public let intent: EvieIntent?
    public let targetDeviceId: String?
    public let targets: [String]?
    public let targetCount: Int?
    public let chain: [String]?
    public let firstTargetDeviceId: String?
    public let transform: String?
    public let sensor: String?
    public let risk: String?
    public let approvalRequired: Bool?
    public let verb: String?
    public let threadId: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case intentId = "intent_id"
        case intent
        case targetDeviceId = "target_device_id"
        case targets
        case targetCount = "target_count"
        case chain
        case firstTargetDeviceId = "first_target_device_id"
        case transform, sensor, risk, verb, threadId
        case approvalRequired = "approval_required"
    }
}

public struct EvieMeshStatus: Codable, Sendable {
    public let ok: Bool
    public let serviceUuid: String
    public let zones: [String: Int]
    public let advertisements: [String: EvieMeshAdvertisementRecord]
    public let macVerbs: [String: String]
    public let sensors: [String: String]
    public let storage: String

    enum CodingKeys: String, CodingKey {
        case ok
        case serviceUuid = "service_uuid"
        case zones
        case advertisements
        case macVerbs = "mac_verbs"
        case sensors, storage
    }
}

private struct MeshObserveBody: Encodable {
    let observations: [EvieProximityObservation]
}

private struct MeshAdvertiseBody: Encodable {
    let batteryPercent: Double?
    let lowPower: Bool
    let capabilities: [String]

    enum CodingKeys: String, CodingKey {
        case batteryPercent = "battery_percent"
        case lowPower
        case capabilities
    }
}

private struct MeshReasonBody: Encodable {
    let reason: String
}

private struct MeshShortcutBody: Encodable {
    let shortcut: String
    let args: [String: String]
}

private struct MeshSensorBody: Encodable {
    let sensor: String
}

private struct MeshNudgeBody: Encodable {
    let title: String
    let body: String
}

private struct MeshTransformBody: Encodable {
    let transform: String
    let text: String
    let ttlSeconds: Int

    enum CodingKeys: String, CodingKey {
        case transform, text
        case ttlSeconds = "ttl_seconds"
    }
}

private struct MeshMigrateBody: Encodable {
    let threadId: String
    let fromDeviceId: String?

    enum CodingKeys: String, CodingKey {
        case threadId = "thread_id"
        case fromDeviceId = "from_device_id"
    }
}

private struct MeshVerbBody: Encodable {
    let verb: String
    let arguments: [String: String]
}

extension EVAPIClient {
    /// Report BLE sightings of peers (observer = this device).
    public func observeProximity(observations: [[String: Any]]) async throws -> EvieProximityResponse {
        let body = try JSONSerialization.data(withJSONObject: ["observations": observations])
        let (_, data) = try await send("/v1/everywhere/proximity/observe", method: "POST", body: body)
        return try JSONDecoder().decode(EvieProximityResponse.self, from: data)
    }

    /// Advertise this device's battery / low-power / capabilities.
    public func advertiseMesh(batteryPercent: Double?, lowPower: Bool, capabilities: [String]) async throws -> EvieMeshAdvertiseResponse {
        let body = try encode(
            MeshAdvertiseBody(batteryPercent: batteryPercent, lowPower: lowPower, capabilities: capabilities)
        )
        let (_, data) = try await send("/v1/everywhere/mesh/advertise", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshAdvertiseResponse.self, from: data)
    }

    /// Read the observer-scoped proximity projection.
    public func fetchProximity() async throws -> EvieProximityReadout {
        let (_, data) = try await send("/v1/everywhere/proximity")
        return try JSONDecoder().decode(EvieProximityReadout.self, from: data)
    }

    /// Converge beacon: every live device chimes, flashes, haptics.
    public func converge(reason: String) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshReasonBody(reason: reason))
        let (_, data) = try await send("/v1/everywhere/converge", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Ask the nearest camera device to capture.
    public func photoCapture(reason: String) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshReasonBody(reason: reason))
        let (_, data) = try await send("/v1/everywhere/photo/capture", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Run a named Shortcut / AppIntent on the best device.
    public func runShortcut(name: String, args: [String: String] = [:]) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshShortcutBody(shortcut: name, args: args))
        let (_, data) = try await send("/v1/everywhere/shortcut/run", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Read a sensor from the nearest phone.
    public func readSensor(_ sensor: String) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshSensorBody(sensor: sensor))
        let (_, data) = try await send("/v1/everywhere/sensor/read", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Nudge that walks the escalation chain until acked.
    public func escalateNudge(title: String, body text: String) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshNudgeBody(title: title, body: text))
        let (_, data) = try await send("/v1/everywhere/nudge/escalate", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Route clipboard THROUGH evie (tidy/translate/summarize/speak).
    public func transformClipboard(transform: String, text: String, ttlSeconds: Int = 1800) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshTransformBody(transform: transform, text: text, ttlSeconds: ttlSeconds))
        let (_, data) = try await send("/v1/everywhere/clipboard/transform", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Mid-conversation handoff to another device.
    public func migrateConversation(threadId: String, fromDeviceId: String? = nil) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshMigrateBody(threadId: threadId, fromDeviceId: fromDeviceId))
        let (_, data) = try await send("/v1/everywhere/conversation/migrate", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// Run a Mac verb (R2 verbs require on-device approval).
    public func macVerb(_ verb: String, arguments: [String: String] = [:]) async throws -> EvieMeshIntentResponse {
        let body = try encode(MeshVerbBody(verb: verb, arguments: arguments))
        let (_, data) = try await send("/v1/everywhere/mac/verb", method: "POST", body: body)
        return try JSONDecoder().decode(EvieMeshIntentResponse.self, from: data)
    }

    /// One honest card: what the mesh knows right now.
    public func fetchMeshStatus() async throws -> EvieMeshStatus {
        let (_, data) = try await send("/v1/everywhere/mesh/status")
        return try JSONDecoder().decode(EvieMeshStatus.self, from: data)
    }
}

#endif
