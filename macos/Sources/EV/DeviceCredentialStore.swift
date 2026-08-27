import Foundation
import Security
#if canImport(CryptoKit)
import CryptoKit
#endif

// DURABLE DEVICE CREDENTIAL — Keychain, survives rebuild/update, not in Git/bundle
// Service: com.ev.suit.device-auth (canonical), Account: device:<UUID>, Value: device bearer token
// Normal EV.app startup uses this, NOT EV_MASTER_KEY.
// LAW: read ONCE per process start, then cached. Helpers must not read.
enum DeviceCredentialStore {
    static let service = "com.ev.suit.device-auth"
    static let legacyService = "com.ev.suit"
    static let legacyService2 = "com.ev.suit.device-token"
    // In-memory cache: 1 read per process, no prompt storm
    private static var cache: [String: String] = [:]
    private static let cacheLock = NSLock()

    private static func account(for deviceID: String) -> String {
        // Normalize: store as device:<uuid> to avoid collisions
        if deviceID.hasPrefix("device:") { return deviceID }
        return "device:\(deviceID)"
    }

    // K-3 fallback: app-local 0600 file, used only if Keychain is inherently unreliable for self-signed
    private static var supportDir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("EV", isDirectory: true) ?? URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("EV")
    }
    private static func fileURL(for deviceID: String) -> URL {
        let escaped = deviceID.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: ":", with: "_")
        return supportDir.appendingPathComponent("device-token-\(escaped).cred")
    }
    private static func saveToFile(token: String, for deviceID: String) {
        let url = fileURL(for: deviceID)
        try? FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
        try? token.write(to: url, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
    private static func loadFromFile(for deviceID: String) -> String? {
        let url = fileURL(for: deviceID)
        guard let token = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.count >= 16 ? trimmed : nil
    }
    @discardableResult
    static func save(token: String, for deviceID: String) -> Bool {
        guard !token.isEmpty, token.count >= 16 else { return false }
        let acct = account(for: deviceID)
        let data = Data(token.utf8)
        // Delete existing first for idempotency (all services)
        for svc in [service, legacyService, legacyService2] {
            let dq: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: svc,
                kSecAttrAccount as String: acct,
            ]
            SecItemDelete(dq as CFDictionary)
        }
        // K1 CLEAN: no SecAccess, no SecAccessControl, no userPresence/biometry.
        // Use AfterFirstUnlockThisDeviceOnly, synchronizable false, ThisDeviceOnly.
        // For self-signed stable cert, default ACL trusts creator (EV.app with leaf 142...).
        // Must be created BY EV.app itself (not swift) to be silent for EV.app.
        let addQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecAttrSynchronizable as String: false,
        ]
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        // Always mirror to 0600 file for K-3 fallback (scoped device token only, never master)
        saveToFile(token: token, for: deviceID)
        if status == errSecSuccess {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            return true
        }
        // If Keychain failed but file succeeded, still consider saved (fallback)
        return FileManager.default.fileExists(atPath: fileURL(for: deviceID).path)
    }

    static func load(for deviceID: String) -> String? {
        let acct = account(for: deviceID)
        cacheLock.lock()
        if let cached = cache[acct] {
            cacheLock.unlock()
            return cached
        }
        cacheLock.unlock()
        // Primary: Keychain canonical — silent only, no UI. If it would prompt, fallback to file.
        if let token = load(account: acct, service: service) {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            // Keep file mirror in sync
            saveToFile(token: token, for: deviceID)
            return token
        }
        // Fallback legacy services (migration, one-time) — also silent only
        for svc in [legacyService, legacyService2] {
            if let token = load(account: acct, service: svc) {
                // Migrate to canonical: delete legacy, save canonical cleanly (K1)
                let dq: [String: Any] = [
                    kSecClass as String: kSecClassGenericPassword,
                    kSecAttrService as String: svc,
                    kSecAttrAccount as String: acct,
                ]
                SecItemDelete(dq as CFDictionary)
                _ = save(token: token, for: deviceID)
                return token
            }
        }
        // K-3 fallback: file (0600) if Keychain unavailable or would prompt — SILENT, no UI
        if let token = loadFromFile(for: deviceID) {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            // Opportunistically re-establish Keychain silently from file.
            // This runs in EV.app's process, so the new item will be created BY EV.app
            // with EV.app's designated requirement (com.ev.suit + leaf 142...), making future
            // silent reads succeed without prompt and fixing the underlying item.
            // SecItemAdd for creation does not prompt; it just writes.
            DispatchQueue.global(qos: .utility).async {
                // Only if silent read still fails (item missing or would prompt)
                let stillMissing: Bool = {
                    let q: [String: Any] = [
                        kSecClass as String: kSecClassGenericPassword,
                        kSecAttrService as String: service,
                        kSecAttrAccount as String: account,
                        kSecReturnData as String: true,
                        kSecMatchLimit as String: kSecMatchLimitOne,
                        kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail,
                    ]
                    var it: CFTypeRef?
                    let st = SecItemCopyMatching(q as CFDictionary, &it)
                    return st != errSecSuccess
                }()
                if stillMissing {
                    let data = Data(token.utf8)
                    for svc in [service, legacyService, legacyService2] {
                        let dq: [String: Any] = [
                            kSecClass as String: kSecClassGenericPassword,
                            kSecAttrService as String: svc,
                            kSecAttrAccount as String: account,
                        ]
                        SecItemDelete(dq as CFDictionary)
                    }
                    let add: [String: Any] = [
                        kSecClass as String: kSecClassGenericPassword,
                        kSecAttrService as String: service,
                        kSecAttrAccount as String: account,
                        kSecValueData as String: data,
                        kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                        kSecAttrSynchronizable as String: false,
                    ]
                    SecItemAdd(add as CFDictionary, nil)
                }
            }
            return token
        }
        return nil
    }

    private static func load(account: String, service: String) -> String? {
        // Silent only — if it would prompt, return nil and let caller fallback to file.
        // NEVER prompt during normal startup. Underlying item must be fixed to be silent.
        let silentQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(silentQuery as CFDictionary, &item)
        if status == errSecInteractionNotAllowed {
            // Would have prompted — treat as missing and fallback to file, do not prompt.
            return nil
        }
        guard status == errSecSuccess, let data = item as? Data, let token = String(data: data, encoding: .utf8),
              !token.isEmpty else { return nil }
        return token
    }

    @discardableResult
    static func delete(for deviceID: String) -> Bool {
        let acct = account(for: deviceID)
        cacheLock.lock()
        cache.removeValue(forKey: acct)
        cacheLock.unlock()
        var ok = false
        for svc in [service, legacyService, legacyService2] {
            let q: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: svc,
                kSecAttrAccount as String: acct,
            ]
            let s = SecItemDelete(q as CFDictionary)
            if s == errSecSuccess { ok = true }
        }
        try? FileManager.default.removeItem(at: fileURL(for: deviceID))
        return ok
    }

    // For diagnostics: fingerprint without value (uses cache, no extra Keychain read if already cached)
    static func fingerprint(for deviceID: String) -> String? {
        guard let token = load(for: deviceID) else { return nil }
        let data = Data(token.utf8)
        #if canImport(CryptoKit)
        let digest = SHA256.hash(data: data)
        return String(digest.map { String(format: "%02x", $0) }.joined().prefix(16))
        #else
        return String(token.prefix(4))
        #endif
    }

    static func hasCredential(for deviceID: String) -> Bool {
        return load(for: deviceID) != nil
    }

    // For startup diagnostics: how many Keychain reads this process has done
    static var readsInThisProcess: Int {
        cacheLock.lock()
        defer { cacheLock.unlock() }
        return cache.count // approximate; real count tracked via call site
    }
}
