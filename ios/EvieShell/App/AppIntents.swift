import AppIntents
import EvieNativeBroker
import Foundation
import UniformTypeIdentifiers

struct TalkWithEvieIntent: AppIntent {
    static var title: LocalizedStringResource = "Talk with Evie"
    static var description = IntentDescription("Open Evie on this iPhone.")
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        .result()
    }
}

struct CaptureForEvieIntent: AppIntent {
    static var title: LocalizedStringResource = "Capture for Evie"
    static var description = IntentDescription("Queue a note for Evie on this iPhone.")
    static var openAppWhenRun = true

    @Parameter(title: "Note")
    var note: String

    func perform() async throws -> some IntentResult {
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .result() }
        let key = UUID().uuidString
        let plan = EvieCaptureInterpreter.interpret(trimmed)
        UserDefaults.standard.set(trimmed, forKey: "evie.pending_capture")
        UserDefaults.standard.set(key, forKey: "evie.pending_capture_key")
        if let token = DeviceAuth.token() {
            var payload: [String: Any] = [
                "text": trimmed,
                "executed": false,
                "interpreted_intent": plan.intent.rawValue,
                "interpreted_title": plan.title,
                "interpreted_confidence": plan.confidence,
            ]
            if let delay = plan.delaySeconds { payload["delay_seconds"] = delay }
            _ = await GatewayClient.post(
                origin: AppOrigin.apiOrigin,
                path: "/v1/device-gateway/queue",
                token: token,
                body: [
                    "idempotency_key": key,
                    "kind": "siri_capture",
                    "payload": payload,
                ]
            )
        }
        return .result()
    }
}

private enum EvieShortcutCall {
    static func tokenOrDialog() -> String? {
        DeviceAuth.token()
    }

    static func detail(_ json: [String: Any]) -> String {
        if let reply = json["reply"] as? String, !reply.isEmpty { return reply }
        if let detail = json["detail"] as? String, !detail.isEmpty { return detail }
        if let error = json["error"] as? String, !error.isEmpty { return error }
        return "Evie did not answer."
    }

    static func clipped(_ text: String) -> String {
        text.count <= 500 ? text : String(text.prefix(500))
    }
}

struct AskEvieIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Evie"
    static var description = IntentDescription("Send a question to Evie on your Mac and speak the reply.")
    static var openAppWhenRun = false

    @Parameter(title: "Question")
    var question: String

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return .result(dialog: "Say what you want to ask Evie.")
        }
        guard let token = EvieShortcutCall.tokenOrDialog() else {
            return .result(dialog: "This iPhone is not paired. Open Evie and pair it, then try again.")
        }
        let result = await GatewayClient.send(
            origin: AppOrigin.apiOrigin,
            path: "/v1/device-gateway/text",
            token: token,
            method: "POST",
            body: [
                "text": trimmed,
                "instance_id": "shortcut",
                "idempotency_key": UUID().uuidString,
            ]
        )
        guard let result else {
            return .result(dialog: "Evie did not answer. Check Tailscale and that the Mac is awake.")
        }
        return .result(dialog: IntentDialog(stringLiteral: EvieShortcutCall.clipped(EvieShortcutCall.detail(result.json))))
    }
}

struct SendLatestScreenshotIntent: AppIntent {
    static var title: LocalizedStringResource = "Send latest screenshot"
    static var description = IntentDescription("Attach a screenshot to a turn on your Mac. In Shortcuts, pass Get Latest Screenshots into this action.")
    static var openAppWhenRun = false

    @Parameter(title: "Screenshot", supportedContentTypes: [.image])
    var image: IntentFile

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let token = EvieShortcutCall.tokenOrDialog() else {
            return .result(dialog: "This iPhone is not paired. Open Evie and pair it, then try again.")
        }
        let data: Data
        if let url = image.fileURL {
            data = try Data(contentsOf: url)
        } else {
            data = try await image.data(contentType: .image)
        }
        guard !data.isEmpty else {
            return .result(dialog: "That screenshot was empty.")
        }
        var name = image.filename.isEmpty ? "screenshot.jpg" : image.filename
        let ext = (name as NSString).pathExtension.lowercased()
        if !["png", "jpg", "jpeg", "heic", "gif", "webp"].contains(ext) {
            name = "screenshot.jpg"
        }
        let attached = await GatewayClient.send(
            origin: AppOrigin.apiOrigin,
            path: "/v1/fabric/attach",
            token: token,
            method: "POST",
            body: [
                "filename": name,
                "content_base64": data.base64EncodedString(),
                "turn_id": "shortcut",
                "kind": "screenshot",
            ]
        )
        guard let attached, (200..<300).contains(attached.status),
              let artifact = attached.json["artifact_id"] as? String, !artifact.isEmpty,
              attached.json["sha256"] is String else {
            let why = attached.map { EvieShortcutCall.detail($0.json) } ?? "Evie did not accept the image."
            return .result(dialog: IntentDialog(stringLiteral: EvieShortcutCall.clipped(why)))
        }
        let turn = await GatewayClient.send(
            origin: AppOrigin.apiOrigin,
            path: "/v1/device-gateway/text",
            token: token,
            method: "POST",
            body: [
                "text": "Here is my latest screenshot.",
                "instance_id": "shortcut",
                "idempotency_key": UUID().uuidString,
                "attachment_ids": [artifact],
            ]
        )
        guard let turn else {
            return .result(dialog: "The screenshot was stored but Evie did not answer the turn.")
        }
        return .result(dialog: IntentDialog(stringLiteral: EvieShortcutCall.clipped(EvieShortcutCall.detail(turn.json))))
    }
}

struct ApprovePendingIntent: AppIntent {
    static var title: LocalizedStringResource = "Approve pending"
    static var description = IntentDescription("Approve the oldest parked Evie action, or a specific one.")
    static var openAppWhenRun = false

    @Parameter(title: "Action id")
    var actionId: String?

    func perform() async throws -> some IntentResult & ProvidesDialog {
        .result(dialog: IntentDialog(stringLiteral: await EvieApprovalIntent.decide(approve: true, actionId: actionId)))
    }
}

struct DenyPendingIntent: AppIntent {
    static var title: LocalizedStringResource = "Deny pending"
    static var description = IntentDescription("Deny the oldest parked Evie action, or a specific one.")
    static var openAppWhenRun = false

    @Parameter(title: "Action id")
    var actionId: String?

    func perform() async throws -> some IntentResult & ProvidesDialog {
        .result(dialog: IntentDialog(stringLiteral: await EvieApprovalIntent.decide(approve: false, actionId: actionId)))
    }
}

private enum EvieApprovalIntent {
    static func decide(approve: Bool, actionId: String?) async -> String {
        guard let token = DeviceAuth.token() else {
            return "This iPhone is not paired. Open Evie and pair it, then try again."
        }
        let chosen = actionId?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let id: String
        if chosen.isEmpty {
            let pending = await GatewayClient.send(
                origin: AppOrigin.apiOrigin,
                path: "/v1/fabric/approvals/pending",
                token: token,
                method: "GET"
            )
            guard let pending, (200..<300).contains(pending.status) else {
                return "Evie could not list pending approvals."
            }
            let rows = pending.json["approvals"] as? [[String: Any]] ?? []
            let oldest = rows.last?["id"] as? String
            guard let oldest else {
                return "Nothing is waiting for approval."
            }
            id = oldest
        } else {
            id = chosen
        }
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let path = "/v1/fabric/approvals/\(encoded)/" + (approve ? "approve" : "deny")
        let result = await GatewayClient.send(
            origin: AppOrigin.apiOrigin,
            path: path,
            token: token,
            method: "POST",
            body: [:]
        )
        guard let result else {
            return "Evie did not answer the approval."
        }
        if result.status == 403 {
            return "Evie needs a fresh confirmation for that. Open the phone panel and approve it there."
        }
        if !(200..<300).contains(result.status) {
            return EvieShortcutCall.clipped(EvieShortcutCall.detail(result.json))
        }
        let status = (result.json["status"] as? String) ?? (approve ? "approved" : "denied")
        return (approve ? "Approved. " : "Denied. ") + status
    }
}

struct EvieStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "Status"
    static var description = IntentDescription("Read whether Evie is on, panicked, and how many lanes are registered.")
    static var openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let token = DeviceAuth.token() else {
            return .result(dialog: "This iPhone is not paired. Open Evie and pair it, then try again.")
        }
        let result = await GatewayClient.send(
            origin: AppOrigin.apiOrigin,
            path: "/v1/fabric/status",
            token: token,
            method: "GET"
        )
        guard let result, (200..<300).contains(result.status) else {
            return .result(dialog: "Evie status is unavailable. Check Tailscale and that the Mac is awake.")
        }
        let on = (result.json["fabric_enabled"] as? Bool) == true
        let panic = (result.json["panic"] as? [String: Any])?["panicked"] as? Bool == true
        let lanes = (result.json["lanes"] as? [Any])?.count ?? 0
        let text = "Evie is \(on ? "on" : "off"). Panic is \(panic ? "on" : "off"). \(lanes) lanes."
        return .result(dialog: IntentDialog(stringLiteral: text))
    }
}

struct EvieShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkWithEvieIntent(),
            phrases: ["Talk with \(.applicationName)"],
            shortTitle: "Talk",
            systemImageName: "waveform"
        )
        AppShortcut(
            intent: AskEvieIntent(),
            phrases: ["Ask \(.applicationName)"],
            shortTitle: "Ask Evie",
            systemImageName: "bubble.left"
        )
        AppShortcut(
            intent: SendLatestScreenshotIntent(),
            phrases: ["Send a screenshot to \(.applicationName)"],
            shortTitle: "Send latest screenshot",
            systemImageName: "photo"
        )
        AppShortcut(
            intent: ApprovePendingIntent(),
            phrases: ["Approve pending in \(.applicationName)"],
            shortTitle: "Approve pending",
            systemImageName: "checkmark.circle"
        )
        AppShortcut(
            intent: DenyPendingIntent(),
            phrases: ["Deny pending in \(.applicationName)"],
            shortTitle: "Deny pending",
            systemImageName: "xmark.circle"
        )
        AppShortcut(
            intent: EvieStatusIntent(),
            phrases: ["\(.applicationName) status"],
            shortTitle: "Status",
            systemImageName: "waveform.path.ecg"
        )
        AppShortcut(
            intent: CaptureForEvieIntent(),
            phrases: ["Capture for \(.applicationName)", "Note to \(.applicationName)"],
            shortTitle: "Capture",
            systemImageName: "square.and.pencil"
        )
    }
}

// Spare (voice shortcut) — iPhone-only, backward compat: additive Siri phrase catalog.
// Talk and Capture stay. Ask / screenshot / approve / deny / status call the Mac API.
struct EvieVoiceShortcutPhrases: Sendable {
    static let talkAlternates: [String] = ["Chat with Evie", "Speak with Evie", "Open Evie"]
    static let captureAlternates: [String] = ["Dictate to Evie", "Save a note for Evie"]
}
