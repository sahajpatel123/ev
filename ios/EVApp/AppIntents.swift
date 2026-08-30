#if canImport(AppIntents)
import AppIntents
import EVClient

/// Siri / Shortcuts surface: "Capture with EV" saves a note through the same
/// offline-first capture path as the app.
struct CaptureWithEVIntent: AppIntent {
    static var title: LocalizedStringResource = "Capture with EV"
    static var description = IntentDescription("Save a note to EV memory.")

    @Parameter(title: "Text")
    var text: String

    func perform() async throws -> some IntentResult {
        let config = EVClientAppConfig()
        let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        _ = try await client.capture(
            payload: CapturePayload(text: text, deviceID: config.deviceID)
        )
        return .result()
    }
}

struct CaptureShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: CaptureWithEVIntent(),
            phrases: ["Capture with \(.applicationName)"]
        )
    }
}
#endif
