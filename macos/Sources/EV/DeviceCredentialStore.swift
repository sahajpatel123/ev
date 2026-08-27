import Foundation
import Security
#if canImport(CryptoKit)
import CryptoKit
#endif

// ZERO-PROMPT DEVICE CREDENTIAL STORE (K-1 canonical, macOS)
//
// Service: com.ev.suit.device-auth, Account: device:<UUID>, Value: device bearer token.
// Normal EV.app startup uses this, NOT EV_MASTER_KEY.
//
// Policy (owner law): normal launch must NEVER show a Keychain password prompt.
// The canonical item is created ONLY by the EV.app process itself via plain
// SecItemAdd: class generic-password, accessible AfterFirstUnlockThisDeviceOnly,
// synchronizable false, NO SecAccessControl, NO legacy SecAccess, no interactive
// flags. macOS then grants silent read access to EV's stable designated
// requirement (identifier "com.ev.suit" + "EV Code Signing" certificate), which
// survives rebuilds re-signed by scripts/signing.sh.
//
// Items that predate this policy were created by foreign tools (swift/security
// CLI) with legacy ACL + partition lists; reading them blocks on a
// SecurityAgent password dialog even with kSecUseAuthenticationUIFail. Such
// items are detected by metadata only (never read), then deleted and recreated
// cleanly using the 0600 file mirror — one-time self-heal, zero user
// interaction. Reads happen ONCE per process; everything else uses the cache.
enum DeviceCredentialStore {
    static let service = "com.ev.suit.device-auth"
    static let legacyService = "com.ev.suit"
    static let legacyService2 = "com.ev.suit.device-token"

    private static var cache: [String: String] = [:]
    private static let cacheLock = NSLock()
    private static var keychainReads = 0
    private static var healDone = false

    private static func account(for deviceID: String) -> String {
        if deviceID.hasPrefix("device:") { return deviceID }
        return "device:\(deviceID)"
    }

    // MARK: - K-3 mirror: app-local 0600 file (scoped device token only, never master)

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

    // MARK: - Keychain primitives

    // Metadata-only query: never touches secret data, never prompts.
    private static func metadata(account: String, service svc: String) -> [String: Any]? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: svc,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnAttributes as String: true,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? [String: Any]
    }

    // Clean items carry an explicit kSecAttrAccessible; legacy foreign-ACL items
    // do not. Only clean items are safe to read without risking a dialog.
    private static func isClean(_ meta: [String: Any]) -> Bool {
        meta[kSecAttrAccessible as String] != nil
    }

    // Silent read with UI forbidden. Counted for startup diagnostics.
    private static func silentRead(account: String, service svc: String) -> String? {
        cacheLock.lock()
        keychainReads += 1
        cacheLock.unlock()
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: svc,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data,
              let token = String(data: data, encoding: .utf8), !token.isEmpty else { return nil }
        return token
    }

    // /usr/bin/security can delete legacy foreign-ACL items that SecItemDelete
    // from EV.app cannot touch (it fast-fails with -25244 and never prompts).
    @discardableResult
    private static func cliDelete(account: String, service svc: String) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["delete-generic-password", "-s", svc, "-a", account]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
            return p.terminationStatus == 0
        } catch {
            return false
        }
    }

    // MARK: - One-time self-heal (never reads legacy item data, never prompts)

    private static func healIfNeeded(for deviceID: String) {
        cacheLock.lock()
        let already = healDone
        healDone = true
        cacheLock.unlock()
        if already { return }

        let acct = account(for: deviceID)
        // Legacy services: never read; if any item lingers, drop it silently.
        for legacy in [legacyService, legacyService2] where metadata(account: acct, service: legacy) != nil {
            NSLog("[EV-CRED] removing legacy keychain item service=\(legacy)")
            _ = cliDelete(account: acct, service: legacy)
        }
        guard let meta = metadata(account: acct, service: service) else { return }
        if isClean(meta) { return }
        // Legacy foreign-created canonical item: reading it would block on a
        // SecurityAgent dialog. Replace it from the 0600 mirror without any
        // user interaction.
        NSLog("[EV-CRED] healing legacy keychain item for \(acct) (no prompt)")
        guard let token = loadFromFile(for: deviceID) else {
            NSLog("[EV-CRED] heal skipped: no local mirror token; credential must be re-paired")
            return
        }
        let dq: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
        ]
        SecItemDelete(dq as CFDictionary) // fast-fails on legacy items; harmless
        if metadata(account: acct, service: service) != nil {
            _ = cliDelete(account: acct, service: service)
        }
        _ = save(token: token, for: deviceID)
    }

    // MARK: - Public API

    @discardableResult
    static func save(token: String, for deviceID: String) -> Bool {
        guard !token.isEmpty, token.count >= 16 else { return false }
        let acct = account(for: deviceID)
        let data = Data(token.utf8)
        // Idempotent sweep of this exact account across all historical services.
        for svc in [service, legacyService, legacyService2] {
            let dq: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: svc,
                kSecAttrAccount as String: acct,
            ]
            SecItemDelete(dq as CFDictionary)
        }
        if metadata(account: acct, service: service) != nil {
            _ = cliDelete(account: acct, service: service)
        }
        // Clean canonical item: created by EV.app, bound to EV's stable
        // designated requirement. No SecAccessControl, no legacy SecAccess,
        // no interactive flags: reads are silent and survive rebuilds.
        var addQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: acct,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecAttrSynchronizable as String: false,
        ]
        var status = SecItemAdd(addQuery as CFDictionary, nil)
        if status == errSecDuplicateItem {
            _ = cliDelete(account: acct, service: service)
            status = SecItemAdd(addQuery as CFDictionary, nil)
        }
        // Mirror is kept regardless, as the repair source of last resort.
        saveToFile(token: token, for: deviceID)
        if status == errSecSuccess {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            NSLog("[EV-CRED] canonical item saved clean for \(acct)")
            return true
        }
        NSLog("[EV-CRED] keychain save failed status=\(status); 0600 mirror retained")
        return false
    }

    static func load(for deviceID: String) -> String? {
        let acct = account(for: deviceID)
        cacheLock.lock()
        if let cached = cache[acct] {
            cacheLock.unlock()
            return cached
        }
        cacheLock.unlock()
        healIfNeeded(for: deviceID)
        // Exactly one silent Keychain read per process per account.
        if let token = silentRead(account: acct, service: service) {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            return token
        }
        // K-3 mirror fallback (read-only here; never a prompt).
        if let token = loadFromFile(for: deviceID) {
            if metadata(account: acct, service: service) == nil, UUID(uuidString: deviceID) != nil {
                // One-time rematerialization of the canonical item, created by
                // EV.app itself so future launches read it silently. Only for
                // registry UUID accounts — never creates duplicate accounts.
                if save(token: token, for: deviceID) { return token }
            }
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            return token
        }
        return nil
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
            if SecItemDelete(q as CFDictionary) == errSecSuccess { ok = true }
        }
        if metadata(account: acct, service: service) != nil {
            if cliDelete(account: acct, service: service) { ok = true }
        }
        try? FileManager.default.removeItem(at: fileURL(for: deviceID))
        return ok
    }

    // Diagnostics: fingerprint without value (cache-first, no extra reads).
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

    // Startup diagnostics: actual SecItemCopyMatching(data) calls this process.
    static var readsInThisProcess: Int {
        cacheLock.lock()
        defer { cacheLock.unlock() }
        return keychainReads
    }
}
