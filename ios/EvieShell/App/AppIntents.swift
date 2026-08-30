import AppIntents

struct TalkWithEvieIntent: AppIntent {
    static var title: LocalizedStringResource = "Talk with Evie"
    static var description = IntentDescription("Start a conversation with Evie on this iPhone.")
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        .result()
    }
}

struct EvieShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkWithEvieIntent(),
            phrases: ["Talk with Evie", "Ask Evie"],
            shortTitle: "Talk",
            systemImageName: "waveform"
        )
    }
}
