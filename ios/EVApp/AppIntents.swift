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
        AppShortcut(
            intent: ConvergeWithEVIntent(),
            phrases: ["Converge with \(.applicationName)"]
        )
    }
}

/// Siri / Shortcuts surface: "Converge with EV" fires a
/// converge beacon — every paired device chimes, haptics,
/// and shows the reason.
struct ConvergeWithEVIntent: AppIntent {
    static var title: LocalizedStringResource = "Converge with EV"
    static var description = IntentDescription("Make every Evie device find you.")

    @Parameter(title: "Reason")
    var reason: String

    func perform() async throws -> some IntentResult {
        let config = EVClientAppConfig()
        let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        _ = try await client.converge(reason: reason)
        return .result()
    }
}
#endif
