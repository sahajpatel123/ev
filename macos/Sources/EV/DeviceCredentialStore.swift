import Foundation
import Security
#if canImport(CryptoKit)
import CryptoKit
#endif

// DURABLE DEVICE CREDENTIAL — Keychain, survives rebuild/update, not in Git/bundle
// Service: com.ev.suit, Account: device:<UUID>, Value: device bearer token
// Normal EV.app startup uses this, NOT EV_MASTER_KEY.
enum DeviceCredentialStore {
    static let service = "com.ev.suit"
    static let legacyService = "com.ev.suit.device-token"

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
        // Delete existing first for idempotency
        let deleteQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
        ]
        SecItemDelete(deleteQuery as CFDictionary)
        // Also clear legacy service if present
        let legacyDelete: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: legacyService,
            kSecAttrAccount as String: acct,
        ]
        SecItemDelete(legacyDelete as CFDictionary)

        let addQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        return status == errSecSuccess
    }

    static func load(for deviceID: String) -> String? {
        let acct = account(for: deviceID)
        // Try primary service
        if let token = load(account: acct, service: service) { return token }
        // Fallback legacy
        if let token = load(account: acct, service: legacyService) { return token }
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
        let q1: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
        ]
        let q2: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: legacyService,
            kSecAttrAccount as String: acct,
        ]
        let s1 = SecItemDelete(q1 as CFDictionary)
        let s2 = SecItemDelete(q2 as CFDictionary)
        return s1 == errSecSuccess || s2 == errSecSuccess
    }

    // For diagnostics: fingerprint without value
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
}
