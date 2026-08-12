/// Minimal Keychain-backed token storage shared by the macOS menu-bar app and
/// the iOS app/extension (same ``service`` + ``account`` conventions so a
/// token written by the app is readable by its share extension when they
/// share a keychain-access-group entitlement).

import Foundation
import Security

public enum KeychainTokenError: Error, Sendable {
    case unexpectedStatus(OSStatus)
}

public struct KeychainTokenStore: Sendable {
    public let service: String

    public init(service: String = "com.ev.client.tokens") {
        self.service = service
    }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    private static var accessibility: CFString {
        #if os(macOS)
        kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        #else
        kSecAttrAccessibleAfterFirstUnlock
        #endif
    }

    public func save(token: String, account: String = "api") throws {
        let data = Data(token.utf8)
        var query = baseQuery(account: account)
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = Self.accessibility
        let status = SecItemAdd(query as CFDictionary, nil)
        if status == errSecDuplicateItem {
            var update: [String: Any] = [kSecValueData as String: data]
            update[kSecAttrAccessible as String] = Self.accessibility
            let updateStatus = SecItemUpdate(baseQuery(account: account) as CFDictionary, update as CFDictionary)
            guard updateStatus == errSecSuccess else {
                throw KeychainTokenError.unexpectedStatus(updateStatus)
            }
            return
        }
        guard status == errSecSuccess else {
            throw KeychainTokenError.unexpectedStatus(status)
        }
    }

    public func load(account: String = "api") throws -> String? {
        var query = baseQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainTokenError.unexpectedStatus(status)
        }
        guard let data = result as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    public func delete(account: String = "api") throws {
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        if status == errSecItemNotFound {
            return
        }
        guard status == errSecSuccess else {
            throw KeychainTokenError.unexpectedStatus(status)
        }
    }
}
