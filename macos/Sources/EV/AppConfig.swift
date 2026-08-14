import EVAuth
import Foundation

/// Runtime configuration for the menu-bar app.
///
/// Key resolution is locked in `EVAuth.APIAuthKey`. Do not invert that
/// order or persist short leftovers — that 401s as
/// "Invalid or revoked device token".
struct AppConfig {
    let baseURL: URL
    let apiKey: String
    let deviceID: String

    init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default
    ) {
        let fileValues = Self.loadDotEnv(fileManager: fileManager)

        let urlString =
            environment["EV_API_URL"]
            ?? defaults.string(forKey: "EV_API_URL")
            ?? fileValues["EV_API_URL"]
            ?? "http://127.0.0.1:8000"
        baseURL = URL(string: urlString) ?? URL(string: "http://127.0.0.1:8000")!

        // DO NOT CHANGE — see EVAuth/APIAuthKey.swift
        let resolvedKey = APIAuthKey.resolve(
            environment: environment,
            fileValues: fileValues,
            defaultsKey: defaults.string(forKey: "EV_API_KEY")
        )
        apiKey = resolvedKey

        let hostName = (Host.current().localizedName ?? "Mac")
            .lowercased()
            .replacingOccurrences(of: " ", with: "-")
        deviceID =
            environment["EV_DEVICE_ID"]
            ?? defaults.string(forKey: "EV_DEVICE_ID")
            ?? fileValues["EV_DEVICE_ID"]
            ?? "mac-\(hostName)"

        if APIAuthKey.isUsable(resolvedKey) {
            defaults.set(resolvedKey, forKey: "EV_API_KEY")
            defaults.set(urlString, forKey: "EV_API_URL")
            defaults.set(deviceID, forKey: "EV_DEVICE_ID")
        } else {
            defaults.removeObject(forKey: "EV_API_KEY")
        }
    }

    var usesPlaceholderKey: Bool {
        !APIAuthKey.isUsable(apiKey)
    }

    static func isUsableAPIKey(_ value: String) -> Bool {
        APIAuthKey.isUsable(value)
    }

    private static func loadDotEnv(fileManager: FileManager) -> [String: String] {
        var merged: [String: String] = [:]
        for url in dotenvURLs(fileManager: fileManager) {
            guard let text = try? String(contentsOf: url, encoding: .utf8) else { continue }
            for (key, value) in parseDotEnv(text) {
                if merged[key] == nil {
                    merged[key] = value
                }
            }
        }
        return merged
    }

    private static func dotenvURLs(fileManager: FileManager) -> [URL] {
        let home = fileManager.homeDirectoryForCurrentUser
        var urls: [URL] = [
            home.appendingPathComponent("Library/Application Support/EV/api.env"),
            home.appendingPathComponent("Library/Application Support/EV/.env"),
            home.appendingPathComponent(".ev/env"),
            home.appendingPathComponent(".ev/.env"),
            home.appendingPathComponent("Code/ev/.env"),
        ]
        var dir = URL(fileURLWithPath: fileManager.currentDirectoryPath)
        for _ in 0..<6 {
            urls.append(dir.appendingPathComponent(".env"))
            dir.deleteLastPathComponent()
        }
        if let exec = Bundle.main.executableURL {
            var parent = exec.deletingLastPathComponent()
            for _ in 0..<8 {
                parent.deleteLastPathComponent()
                urls.append(parent.appendingPathComponent(".env"))
            }
        }
        var seen = Set<String>()
        return urls.filter { url in
            seen.insert(url.path).inserted
        }
    }

    static func parseDotEnv(_ text: String) -> [String: String] {
        var values: [String: String] = [:]
        for rawLine in text.split(whereSeparator: \.isNewline) {
            var line = String(rawLine).trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            if line.hasPrefix("export ") {
                line = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
            }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if (value.hasPrefix("\"") && value.hasSuffix("\""))
                || (value.hasPrefix("'") && value.hasSuffix("'")) {
                value = String(value.dropFirst().dropLast())
            }
            if key.hasPrefix("EV_") {
                values[key] = value
            }
        }
        return values
    }
}
