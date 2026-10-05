import Foundation

public struct AppLaunchEntry: Sendable, Equatable {
    public let appID: String
    public let displayName: String
    public let aliases: [String]
    public let universalLinks: [String]
    public let urlSchemes: [String]
    public let fallbackWebURL: String?

    public var launchURL: String? {
        if let first = universalLinks.first { return first }
        if let fallback = fallbackWebURL { return fallback }
        return urlSchemes.first
    }
}

public enum AppLaunchRegistry {
    public static let entries: [AppLaunchEntry] = [
        .init(appID: "safari", displayName: "Safari", aliases: ["safari", "browser"], universalLinks: ["https://www.apple.com"], urlSchemes: [], fallbackWebURL: "https://www.apple.com"),
        .init(appID: "maps", displayName: "Maps", aliases: ["maps", "apple maps"], universalLinks: ["https://maps.apple.com/"], urlSchemes: [], fallbackWebURL: "https://maps.apple.com"),
        .init(appID: "spotify", displayName: "Spotify", aliases: ["spotify"], universalLinks: ["https://open.spotify.com"], urlSchemes: [], fallbackWebURL: "https://open.spotify.com"),
        .init(appID: "instagram", displayName: "Instagram", aliases: ["instagram", "insta"], universalLinks: ["https://www.instagram.com"], urlSchemes: [], fallbackWebURL: "https://www.instagram.com"),
        .init(appID: "youtube", displayName: "YouTube", aliases: ["youtube", "yt"], universalLinks: ["https://www.youtube.com"], urlSchemes: [], fallbackWebURL: "https://www.youtube.com"),
        .init(appID: "gmail", displayName: "Gmail", aliases: ["gmail"], universalLinks: ["https://mail.google.com"], urlSchemes: [], fallbackWebURL: "https://mail.google.com"),
        .init(appID: "whatsapp", displayName: "WhatsApp", aliases: ["whatsapp"], universalLinks: ["https://wa.me"], urlSchemes: [], fallbackWebURL: "https://wa.me"),
        .init(appID: "chrome", displayName: "Chrome", aliases: ["chrome", "google chrome"], universalLinks: ["https://www.google.com"], urlSchemes: [], fallbackWebURL: "https://www.google.com"),
        .init(appID: "music", displayName: "Music", aliases: ["music", "apple music"], universalLinks: ["https://music.apple.com"], urlSchemes: [], fallbackWebURL: "https://music.apple.com"),
        .init(appID: "x", displayName: "X", aliases: ["x", "twitter"], universalLinks: ["https://x.com"], urlSchemes: [], fallbackWebURL: "https://x.com"),
    ]

    /// Dynamic custom entries registered at runtime (persisted via UserDefaults
    /// under "evie.custom_apps" as appID → launch URL). Lets the owner teach
    /// Evie a new app without a code change; exact registry matches win.
    public static var customEntries: [AppLaunchEntry] {
        guard let dict = UserDefaults.standard.dictionary(forKey: "evie.custom_apps") as? [String: String] else { return [] }
        return dict.compactMap { (key, url) in
            let name = key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !name.isEmpty, URL(string: url) != nil else { return nil }
            return AppLaunchEntry(appID: name.lowercased(), displayName: name, aliases: [name.lowercased()], universalLinks: [url], urlSchemes: [], fallbackWebURL: url)
        }.sorted { $0.appID < $1.appID }
    }

    public static func registerCustomApp(name: String, url: String) -> Bool {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, URL(string: url) != nil else { return false }
        var dict = (UserDefaults.standard.dictionary(forKey: "evie.custom_apps") as? [String: String]) ?? [:]
        dict[trimmed] = url
        UserDefaults.standard.set(dict, forKey: "evie.custom_apps")
        return true
    }

    public static func removeCustomApp(name: String) {
        var dict = (UserDefaults.standard.dictionary(forKey: "evie.custom_apps") as? [String: String]) ?? [:]
        dict.removeValue(forKey: name)
        UserDefaults.standard.set(dict, forKey: "evie.custom_apps")
    }

    public static func resolve(_ query: String) -> AppLaunchEntry? {
        candidates(for: query, limit: 1).first
    }

    /// Ranked candidates: exact → prefix → substring → custom entries.
    /// Returns up to `limit` entries so callers can surface ambiguity instead
    /// of guessing. Dynamic, not static: handles any phrasing in the field.
    public static func candidates(for query: String, limit: Int = 4) -> [AppLaunchEntry] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return [] }
        let all = entries + customEntries
        var scored: [(AppLaunchEntry, Int)] = []
        for entry in all {
            let keys = Set([entry.appID, entry.displayName.lowercased()] + entry.aliases)
            if keys.contains(needle) { scored.append((entry, 0)); continue }
            if keys.contains(where: { $0.hasPrefix(needle) || needle.hasPrefix($0) }) { scored.append((entry, 1)); continue }
            if keys.contains(where: { $0.contains(needle) || needle.contains($0) }) { scored.append((entry, 2)); continue }
        }
        return scored.sorted { $0.1 < $1.1 }.prefix(max(1, limit)).map { $0.0 }
    }
}

// Cycle 49 — iPhone-only, backward compat: launch-registry Siri phrase list.
// Additive lookup helper; AppLaunchRegistry.resolve(_:) unchanged.
public enum EvieSiriPhraseList {
    public static let phrasesByAppID: [String: [String]] = [
        "safari": ["Open Safari", "Browse with Safari"],
        "maps": ["Open Maps", "Get directions with Maps"],
        "spotify": ["Open Spotify", "Play music on Spotify"],
        "instagram": ["Open Instagram"],
        "youtube": ["Open YouTube", "Watch on YouTube"],
        "gmail": ["Open Gmail", "Check Gmail"],
        "whatsapp": ["Open WhatsApp", "Message on WhatsApp"],
        "chrome": ["Open Chrome"],
        "music": ["Open Music", "Play music"],
        "x": ["Open X"],
    ]

    public static func phrases(forAppID appID: String) -> [String] {
        if let fixed = phrasesByAppID[appID], !fixed.isEmpty { return fixed }
        let all = AppLaunchRegistry.entries + AppLaunchRegistry.customEntries
        guard let entry = all.first(where: { $0.appID == appID }) else { return [] }
        return dynamicPhrases(for: entry)
    }

    /// Dynamic fallback: any registry entry — including owner-registered
    /// custom apps — gets open/launch phrases from its display name, so new
    /// apps are voice-addressable without a code change.
    public static func dynamicPhrases(for entry: AppLaunchEntry) -> [String] {
        let name = entry.displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return [] }
        return ["Open \(name)", "Launch \(name)", "Ask Evie to open \(name)"]
    }

    /// Every known phrase across built-in and custom apps.
    public static func allPhrases() -> [String] {
        let all = AppLaunchRegistry.entries + AppLaunchRegistry.customEntries
        var seen: [String] = []
        for entry in all {
            for phrase in phrases(forAppID: entry.appID) where !seen.contains(phrase) {
                seen.append(phrase)
            }
        }
        return seen
    }
}
