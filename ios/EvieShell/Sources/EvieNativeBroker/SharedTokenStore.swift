import Foundation
import Security

/// One token convention for every native Evie surface.
///
/// `EVClient.KeychainTokenStore` (EVApp / Watch / Share / macOS menu bar) has
/// always used service `com.ev.client.tokens` + account `api`, landing in the
/// `com.ev.ios` keychain group on iOS. The shell used to keep a second,
/// unreachable identity (`com.ev.evie.shell` / `device_bearer`); it now reads
/// and writes this shared convention, with a one-way migration that moves a
/// legacy shell token into the shared slot on first sight.
///
/// Cross-app sharing needs the `com.ev.ios` group in every app's
/// keychain-access-groups entitlement plus an explicit `kSecAttrAccessGroup`
/// built from the runtime `AppIdentifierPrefix` (exposed via Info.plist, as
/// `$(AppIdentifierPrefix)` is a build setting, not a runtime API). When the
/// prefix is unavailable (macOS dev runs, harness), the group is omitted and
/// the store stays consistent within the running app.
public enum SharedTokenConvention {
    public static let service = "com.ev.client.tokens"
    public static let account = "api"
    public static let groupSuffix = "com.ev.ios"

    public static let legacyService = "com.ev.evie.shell"
    public static let legacyAccount = "device_bearer"

    /// `AppIdentifierPrefix` values arrive with a trailing dot
    /// (`"ABC123D4EF."`); tolerate missing/blank input by returning nil.
    public static func resolveAccessGroup(prefix: String?) -> String? {
        guard let trimmed = prefix?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else { return nil }
        let dotted = trimmed.hasSuffix(".") ? trimmed : trimmed + "."
        return dotted + groupSuffix
    }

    public static func appIdentifierPrefix(bundle: Bundle = .main) -> String? {
        bundle.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String
    }
}

/// Thin `SecItem*` wrapper over one service/account/group triple.
/// Mirrors `EVClient.KeychainTokenStore` semantics; the shell package cannot
/// depend on EVClient, so the triple (not the type) is the shared contract.
public struct SharedTokenStore: Sendable {
    public let service: String
    public let account: String
    public let accessGroup: String?

    public init(service: String, account: String, accessGroup: String? = nil) {
        self.service = service
        self.account = account
        self.accessGroup = accessGroup
    }

    /// The cross-app token slot. Pass the running app's prefix explicitly so
    /// callers (and tests) never depend on ambient bundle state.
    public static func shared(accessGroupPrefix: String?) -> SharedTokenStore {
        SharedTokenStore(
            service: SharedTokenConvention.service,
            account: SharedTokenConvention.account,
            accessGroup: SharedTokenConvention.resolveAccessGroup(prefix: accessGroupPrefix)
        )
    }

    public static func legacyShell() -> SharedTokenStore {
        SharedTokenStore(
            service: SharedTokenConvention.legacyService,
            account: SharedTokenConvention.legacyAccount,
            accessGroup: nil
        )
    }

    private func baseQuery() -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }

    @discardableResult
    public func save(token: String) -> Bool {
        var query = baseQuery()
        query[kSecValueData as String] = Data(token.utf8)
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(query as CFDictionary, nil)
        if status == errSecDuplicateItem {
            let update: [String: Any] = [kSecValueData as String: Data(token.utf8)]
            return SecItemUpdate(baseQuery() as CFDictionary, update as CFDictionary) == errSecSuccess
        }
        return status == errSecSuccess
    }

    public func load() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func delete() {
        SecItemDelete(baseQuery() as CFDictionary)
    }
}
