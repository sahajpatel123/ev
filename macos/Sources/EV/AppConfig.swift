import EVAuth
import Foundation

/// Runtime configuration for the menu-bar app.
///
/// Key resolution is locked in `EVAuth.APIAuthKey`. Do not invert that
/// order or persist short leftovers — that 401s as
/// "Invalid or revoked device token".
struct AppConfig {
    let baseURL: URL
    let baseURLSource: String
    let apiKey: String
    let deviceID: String

    init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default
    ) {
        let fileValues = Self.loadDotEnv(fileManager: fileManager)

        let urlSelection: (value: String, source: String)
        if let value = environment["EV_API_URL"], !value.isEmpty {
            urlSelection = (value, "process environment EV_API_URL")
        } else if let value = defaults.string(forKey: "EV_API_URL"), !value.isEmpty {
            urlSelection = (value, "UserDefaults EV_API_URL")
        } else if let value = fileValues["EV_API_URL"], !value.isEmpty {
            urlSelection = (value, "dotenv EV_API_URL")
        } else {
            urlSelection = ("http://127.0.0.1:8000", "built-in default")
        }
        let urlString = urlSelection.value
        baseURLSource = urlSelection.source
        baseURL = URL(string: urlString) ?? URL(string: "http://127.0.0.1:8000")!

        let hostName = (Host.current().localizedName ?? "Mac")
            .lowercased()
            .replacingOccurrences(of: " ", with: "-")
        deviceID =
            environment["EV_DEVICE_ID"]
            ?? defaults.string(forKey: "EV_DEVICE_ID")
            ?? fileValues["EV_DEVICE_ID"]
            ?? "mac-\(hostName)"

        // PRIMARY: durable device token from Keychain (survives rebuild, not in Git)
        // Normal EV.app startup MUST use this, not EV_MASTER_KEY.
        if let registryID = defaults.string(forKey: "EV_REGISTRY_DEVICE_ID"),
           let token = DeviceCredentialStore.load(for: registryID),
           APIAuthKey.isUsable(token) {
            apiKey = token
            // Keep URL and deviceID persisted, but do NOT persist token to UserDefaults
            // (Keychain is the authority). Clean legacy UserDefaults master if stale.
            defaults.set(urlString, forKey: "EV_API_URL")
            defaults.set(deviceID, forKey: "EV_DEVICE_ID")
            // If UserDefaults still holds a master-length value that is not the device token,
            // keep it for now but it will not be used (Keychain wins). Do not overwrite Keychain.
            return
        }
        // Also try Keychain for the computed deviceID (pre-registry case)
        if let token = DeviceCredentialStore.load(for: deviceID),
           APIAuthKey.isUsable(token) {
            apiKey = token
            defaults.set(urlString, forKey: "EV_API_URL")
            defaults.set(deviceID, forKey: "EV_DEVICE_ID")
            return
        }

        // FALLBACK: legacy master-key path (only for first pairing or repair)
        // This will be deprecated once all trusted devices have Keychain tokens.
        let resolvedKey = APIAuthKey.resolve(
            environment: environment,
            fileValues: fileValues,
            defaultsKey: defaults.string(forKey: "EV_API_KEY")
        )
        apiKey = resolvedKey

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

    /// Production wake gate OFF | SHADOW | ON. OFF = no always-on mic, SHADOW = local KWS only, ON = local KWS + accepted-wake handoff.
    /// Sources: env, UserDefaults, then dotenv, default OFF.
    var alwaysAvailableWake: String {
        let env = ProcessInfo.processInfo.environment["EV_ALWAYS_AVAILABLE_WAKE"]
        if let env, !env.isEmpty { return env.uppercased() }
        if let v = UserDefaults.standard.string(forKey: "EV_ALWAYS_AVAILABLE_WAKE"), !v.isEmpty { return v.uppercased() }
        let fileValues = Self.loadDotEnv()
        if let v = fileValues["EV_ALWAYS_AVAILABLE_WAKE"], !v.isEmpty { return v.uppercased() }
        return "OFF"
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
