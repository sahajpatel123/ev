// Cycle EAC-13 — iPhone Siri phrase catalog.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Centralizes the spoken Siri / Shortcuts phrases for both iPhones so the
/// App Intent ("Capture with EV") and the shell shortcut stay in sync.
/// Pure strings — the Xcode-only AppIntents target reads from here; no
/// AppIntents import so CLT validation still compiles.

import Foundation

public enum EvieSiriPhrases: Sendable {
    public static let capturePhrases: [String] = [
        "Capture with EV",
        "Remember this with EV",
        "Save to EV",
    ]

    public static let askPhrases: [String] = [
        "Ask EV",
        "Ask EVIE",
        "Check with EV",
    ]

    public static let voicePhrases: [String] = [
        "Talk to EV",
        "Start EV live",
    ]

    /// Every phrase the iPhone registers with Siri / Shortcuts, deduplicated.
    public static func allPhrases() -> [String] {
        var seen = Set<String>()
        var ordered: [String] = []
        for phrase in capturePhrases + askPhrases + voicePhrases {
            let trimmed = phrase.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, seen.insert(trimmed).inserted else { continue }
            ordered.append(trimmed)
        }
        return ordered
    }

    /// True when a heard Siri phrase should route to capture.
    public static func isCapturePhrase(_ heard: String) -> Bool {
        let norm = heard.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return capturePhrases.contains { $0.lowercased() == norm }
    }
}
