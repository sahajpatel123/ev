import AppIntents
import EvieNativeBroker
import Foundation

struct TalkWithEvieIntent: AppIntent {
    static var title: LocalizedStringResource = "Talk with Evie"
    static var description = IntentDescription("Start a conversation with Evie on this iPhone.")
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

struct EvieShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkWithEvieIntent(),
            phrases: ["Talk with \(.applicationName)", "Ask \(.applicationName)"],
            shortTitle: "Talk",
            systemImageName: "waveform"
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
// Existing TalkWithEvieIntent / CaptureForEvieIntent / EvieShortcuts unchanged.
struct EvieVoiceShortcutPhrases: Sendable {
    static let talkAlternates: [String] = ["Chat with Evie", "Speak with Evie", "Open Evie"]
    static let captureAlternates: [String] = ["Dictate to Evie", "Save a note for Evie"]
}
