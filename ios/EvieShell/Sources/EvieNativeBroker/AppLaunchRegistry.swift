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

    public static func resolve(_ query: String) -> AppLaunchEntry? {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if needle.isEmpty { return nil }
        let exact = entries.filter {
            Set([$0.appID, $0.displayName.lowercased()] + $0.aliases).contains(needle)
        }
        if exact.count == 1 { return exact[0] }
        return nil
    }
}
