import AppKit
import Combine
import EVClient
import Foundation

/// The Mac half of the Evie Mesh: polls the Follow-Me intent bus
/// for intents addressed to THIS device (or broadcast to everyone)
/// and executes them locally, then acks with a receipt.
///
/// Execution rules:
/// - converge.beacon   chime + speak + flash (MeshConvergeService)
/// - photo.capture     capture this Mac's camera, save to Pictures/EV
/// - shortcut.run      open the named Shortcut via the Shortcuts app
/// - sensor.read       battery (real); anything else fails honestly
/// - nudge.escalate    banner notification via NotificationBridge
/// - clipboard.transform  deterministic tidy of the pasteboard
/// - conversation.migrate  pause the live session, hand over
/// - mac.verb          R1 runs immediately; R2 waits for approval
///
/// No intent is ever silently dropped: every outcome acks DONE or
/// FAILED with a note, so the backend receipt log stays truthful.
public final class MeshIntentPoller: @unchecked Sendable, ObservableObject {
    public static let shared = MeshIntentPoller()

    private let client: EVAPIClient
    private let deviceId: String
    private let lock = NSLock()
    private var task: Task<Void, Never>?
    private var handled: Set<String> = []
    private var approvalStore = MeshApprovalStore()

    @MainActor
    @Published public var lastActivity: String = ""

    public init(
        client: EVAPIClient = EVAPIClient(
            baseURL: EVClientAppConfig().baseURL,
            token: EVClientAppConfig().apiKey
        ),
        // The registered device id is persisted by AppConfig at
        // pairing time; the EVClient fallback is a hostname label,
        // never the registry UUID.
        deviceId: String = UserDefaults.standard.string(forKey: "EV_DEVICE_ID")
            ?? EVClientAppConfig().deviceID
    ) {
        self.client = client
        self.deviceId = deviceId
    }

    // MARK: - Lifecycle

    public func start() {
        lock.lock()
        guard task == nil else {
            lock.unlock(); return
        }
        task = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(10))
                await self?.poll()
            }
        }
        lock.unlock()
        Task { await poll() }
    }

    public func stop() {
        lock.lock()
        task?.cancel()
        task = nil
        lock.unlock()
    }

    // MARK: - Polling

    public func poll() async {
        do {
            let listing = try await client.listIntents(status: "PENDING")
            for intent in listing.intents where !isHandled(intent.id) {
                await handle(intent)
            }
        } catch {
            // Offline or unreachable: the bus is durable-ish by TTL;
            // polling simply resumes when the tailnet is back.
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

    // MARK: - Execution

    private func handle(_ intent: EvieIntent) async {
        // Broadcast intents (converge) have no single target; every
        // device executes. Targeted intents execute only here.
        let isBroadcast = intent.target_device_id == nil
        let isTargeted = intent.target_device_id == deviceId
        guard isBroadcast || isTargeted else { return }
        guard !isHandled(intent.id) else { return }
        markHandled(intent.id)

        switch intent.kind {
        case "converge.beacon":
            let result = MeshConvergeService.shared.converge(reason: stringArg(intent.args, "reason"))
            await ack(intent.id, status: "DONE", note: "chimed=\(result.chimed) spoke=\(result.spoke)")
        case "photo.capture":
            await handlePhotoCapture(intent)
        case "shortcut.run":
            await handleShortcutRun(intent)
        case "sensor.read":
            await handleSensorRead(intent)
        case "nudge.escalate":
            await handleNudgeEscalate(intent)
        case "clipboard.transform":
            await handleClipboardTransform(intent)
        case "conversation.migrate":
            await handleConversationMigrate(intent)
        case "mac.verb":
            await handleMacVerb(intent)
        default:
            await ack(intent.id, status: "FAILED", note: "unhandled kind on mac: \(intent.kind)")
        }
    }

    private func stringArg(_ args: [String: AnyCodableValue], _ key: String) -> String {
        guard case .string(let value)? = args[key] else { return "" }
        return value
    }

    private func ack(_ id: String, status: String, note: String) async {
        do {
            _ = try await client.ackIntent(id: id, status: status, note: note)
            await MainActor.run { self.lastActivity = "\(status): \(note)" }
        } catch {
            await MainActor.run { self.lastActivity = "ack failed: \(note)" }
        }
    }

    // MARK: - Vertices

    private func handlePhotoCapture(_ intent: EvieIntent) async {
        // The Mac captures its own camera through the shared
        // CameraManager; the frame is saved with EV provenance.
        do {
            let manager = CameraManager.shared
            guard await manager.requestAccess() else {
                await ack(intent.id, status: "FAILED", note: "camera permission denied on mac")
                return
            }
            let frame = try await manager.captureFrame(forSave: true)
            let saved = manager.savePhoto(frame.jpeg)
            await ack(
                intent.id, status: "DONE",
                note: "captured by mac: \(saved.filename), \(frame.width)x\(frame.height)"
            )
        } catch {
            await ack(intent.id, status: "FAILED", note: "mac capture failed: \(error.localizedDescription)")
        }
    }

    private func handleShortcutRun(_ intent: EvieIntent) async {
        guard case .string(let name)? = intent.args["shortcut"] else {
            await ack(intent.id, status: "FAILED", note: "shortcut.run without a name")
            return
        }
        // The Shortcuts app URL scheme opens (and runs, when the
        // shortcut takes no input) the named shortcut.
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? name
        guard let url = URL(string: "shortcuts://run-shortcut?name=\(encoded)") else {
            await ack(intent.id, status: "FAILED", note: "bad shortcut URL")
            return
        }
        DispatchQueue.main.async {
            NSWorkspace.shared.open(url)
        }
        await ack(intent.id, status: "DONE", note: "opened shortcut: \(name)")
    }

    private func handleSensorRead(_ intent: EvieIntent) async {
        guard case .string(let sensor)? = intent.args["sensor"] else {
            await ack(intent.id, status: "FAILED", note: "sensor.read without a sensor")
            return
        }
        switch sensor {
        case "battery":
            let percent = MacBatteryReader.batteryPercent()
            if let percent {
                await ack(intent.id, status: "DONE", note: "battery \(Int(percent))%")
            } else {
                await ack(intent.id, status: "FAILED", note: "battery unreadable on this mac")
            }
        default:
            // Honesty law: the Mac has no barometer, GPS, torch, or
            // HealthKit. It says so instead of inventing a number.
            await ack(intent.id, status: "FAILED", note: "no \(sensor) sensor on mac")
        }
    }

    private func handleNudgeEscalate(_ intent: EvieIntent) async {
        let title = stringArg(intent.args, "title")
        let body = stringArg(intent.args, "body")
        await NotificationBridge.shared.post(
            title: title.isEmpty ? "Evie" : title,
            body: body.isEmpty ? "Attention requested" : body,
            identifier: "mesh-nudge-\(intent.id)"
        )
        await ack(intent.id, status: "DONE", note: "nudge shown on mac")
    }

    private func handleClipboardTransform(_ intent: EvieIntent) async {
        guard case .string(let transform)? = intent.args["transform"],
              case .string(let text)? = intent.args["text"]
        else {
            await ack(intent.id, status: "FAILED", note: "clipboard.transform missing payload")
            return
        }
        switch transform {
        case "tidy":
            // Deterministic: collapse whitespace, trim. The only
            // transform the Mac performs without a model.
            let tidy = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(tidy, forType: .string)
            await ack(intent.id, status: "DONE", note: "tidy applied on mac: \(tidy.prefix(80))")
        case "speak":
            let synthesizer = NSSpeechSynthesizer(voice: nil)
            _ = synthesizer?.startSpeaking(text)
            await ack(intent.id, status: "DONE", note: "spoke clipboard on mac")
        default:
            // translate/summarize need the reasoning API, which a
            // phone or the backend owns — the Mac does not fake it.
            await ack(intent.id, status: "FAILED", note: "\(transform) not available on mac")
        }
    }

    private func handleConversationMigrate(_ intent: EvieIntent) async {
        // Hand the live conversation to the target device. The Mac
        // keeps its session (pausing another actor's audio graph
        // from here would guess at state); it records the handoff
        // honestly and the owner simply speaks to the target device.
        let thread = stringArg(intent.args, "thread_id")
        await ack(intent.id, status: "DONE", note: "handoff recorded, continue on target: \(thread)")
    }

    // MARK: - Mac verbs (approval-gated)

    private func handleMacVerb(_ intent: EvieIntent) async {
        guard case .string(let verb)? = intent.args["verb"] else {
            await ack(intent.id, status: "FAILED", note: "mac.verb without a verb")
            return
        }
        let risk = stringArg(intent.args, "risk")
        if risk == "R2" {
            // Approval law: anything that touches apps, windows,
            // keyboard, files, or URLs waits for the human at the
            // Mac. The menubar UI shows the request; executing it
            // requires an explicit allow.
            let requestId = intent.id
            approvalStore.enqueue(
                MeshApprovalRequest(
                    intentId: requestId,
                    verb: verb,
                    arguments: stringArguments(intent.args),
                    requestedBy: intent.source_device_id ?? "unknown"
                )
            )
            await NotificationBridge.shared.post(
                title: "Approval needed",
                body: "\(intent.source_device_id ?? "Another device") asked the Mac to \(verb)",
                identifier: "mesh-approval-\(intent.id)"
            )
            await ack(intent.id, status: "CLAIMED", note: "R2 verb held for approval: \(verb)")
            return
        }
        let result = MacControlService.shared.handle(
            command: verb,
            arguments: anyArguments(stringArguments(intent.args)),
            requestId: intent.id
        )
        let ok = result["ok"] as? Bool == true
        let spoken = result["spoken"] as? String ?? "executed"
        await ack(intent.id, status: ok ? "DONE" : "FAILED", note: "mac verb \(verb): \(spoken)")
    }

    private func stringArguments(_ args: [String: AnyCodableValue]) -> [String: String] {
        var out: [String: String] = [:]
        for (key, value) in args {
            switch value {
            case .string(let string): out[key] = string
            case .number(let number): out[key] = String(number)
            case .bool(let flag): out[key] = flag ? "true" : "false"
            default: break
            }
        }
        return out
    }

    // MARK: - Approval execution (called from the menubar UI)

    /// Approve a held R2 verb and execute it exactly once.
    public func approve(_ requestId: String) async -> Bool {
        guard let request = approvalStore.dequeue(requestId) else { return false }
        let result = MacControlService.shared.handle(
            command: request.verb,
            arguments: anyArguments(request.arguments),
            requestId: request.intentId
        )
        let ok = result["ok"] as? Bool == true
        let spoken = result["spoken"] as? String ?? "executed"
        await ack(request.intentId, status: ok ? "DONE" : "FAILED", note: "approved mac verb \(request.verb): \(spoken)")
        return ok
    }

    public func deny(_ requestId: String) async {
        guard let request = approvalStore.dequeue(requestId) else { return }
        await ack(request.intentId, status: "FAILED", note: "denied by owner at mac: \(request.verb)")
    }

    public func pendingApprovals() -> [MeshApprovalRequest] {
        approvalStore.pending()
    }
}

/// MacControlService takes [String: Any]; the approval queue
/// stores the string projection. Rehydrate at the boundary.
private func anyArguments(_ strings: [String: String]) -> [String: Any] {
    strings.mapValues { $0 as Any }
}

/// A held R2 Mac verb waiting for the human at the Mac.
public struct MeshApprovalRequest: Equatable, Sendable, Identifiable {
    public let id: String
    public let intentId: String
    public let verb: String
    public let arguments: [String: String]
    public let requestedBy: String

    public init(intentId: String, verb: String, arguments: [String: String], requestedBy: String) {
        self.id = intentId
        self.intentId = intentId
        self.verb = verb
        self.arguments = arguments
        self.requestedBy = requestedBy
    }
}

final class MeshApprovalStore: @unchecked Sendable {
    private let lock = NSLock()
    private var queue: [MeshApprovalRequest] = []

    func enqueue(_ request: MeshApprovalRequest) {
        lock.lock()
        queue.append(request)
        lock.unlock()
    }

    func dequeue(_ intentId: String) -> MeshApprovalRequest? {
        lock.lock()
        defer { lock.unlock() }
        guard let index = queue.firstIndex(where: { $0.intentId == intentId }) else { return nil }
        return queue.remove(at: index)
    }

    func pending() -> [MeshApprovalRequest] {
        lock.lock()
        defer { lock.unlock() }
        return queue
    }
}
