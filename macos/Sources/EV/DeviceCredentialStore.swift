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
        let addQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            // No kSecAttrAccessControl with userPresence — prompt-free
        ]
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        if status == errSecSuccess {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
        }
        return status == errSecSuccess
    }

    static func load(for deviceID: String) -> String? {
        let acct = account(for: deviceID)
        cacheLock.lock()
        if let cached = cache[acct] {
            cacheLock.unlock()
            return cached
        }
        cacheLock.unlock()
        // Try primary service
        if let token = load(account: acct, service: service) {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            return token
        }
        // Fallback legacy services (migration, one-time)
        for svc in [legacyService, legacyService2] {
            if let token = load(account: acct, service: svc) {
                // Migrate to canonical service with correct ACL (no prompt)
                _ = save(token: token, for: deviceID)
                // Clean legacy after migration
                let dq: [String: Any] = [
                    kSecClass as String: kSecClassGenericPassword,
                    kSecAttrService as String: svc,
                    kSecAttrAccount as String: acct,
                ]
                SecItemDelete(dq as CFDictionary)
                return token
            }
        }
        return nil
    }

    private static func load(account: String, service: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
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
