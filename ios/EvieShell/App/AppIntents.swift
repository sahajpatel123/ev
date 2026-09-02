import AppIntents
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
        UserDefaults.standard.set(trimmed, forKey: "evie.pending_capture")
        UserDefaults.standard.set(key, forKey: "evie.pending_capture_key")
        if let token = DeviceAuth.token() {
            _ = await GatewayClient.post(
                origin: AppOrigin.apiOrigin,
                path: "/v1/device-gateway/queue",
                token: token,
                body: [
                    "idempotency_key": key,
                    "kind": "siri_capture",
                    "payload": ["text": trimmed, "executed": false],
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
            phrases: ["Talk with Evie", "Ask Evie"],
            shortTitle: "Talk",
            systemImageName: "waveform"
        ),
        AppShortcut(
            intent: CaptureForEvieIntent(),
            phrases: ["Capture for Evie", "Note to Evie"],
            shortTitle: "Capture",
            systemImageName: "square.and.pencil"
        )
    }
}
