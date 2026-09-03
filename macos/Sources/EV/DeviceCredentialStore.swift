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
// items are never read: a UIFail-guarded probe returns
// errSecInteractionNotAllowed instead, and only THEN is the item deleted and
// recreated from the 0600 mirror — evidence-based, one-time, zero user
// interaction.
//
// WARNING (proven live, macOS 26): kSecAttrAccessible set at SecItemAdd time
// is NOT surfaced in SecItemCopyMatching attribute dictionaries for generic
// passwords. Never infer item "cleanliness" from metadata shape — it made
// earlier versions delete and re-add a healthy item on every launch.
//
// Steady-state normal launch: ONE attributes query (legacy sweep), ONE guarded
// data read (health probe), ZERO keychain writes, zero dialogs; the 0600
// mirror is the hot-path token source and everything is cached per process.
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

    // CLEANLINESS IS NOT DETECTABLE BY METADATA SHAPE on the macOS file
    // keychain: kSecAttrAccessible set at SecItemAdd time is NOT returned in
    // the SecItemCopyMatching attribute dictionary (verified live: keys are
    // acct/cdat/class/labl/mdat/svce only). Any delete-and-recreate heuristic
    // based on that absence rewrites a healthy item on EVERY launch. The
    // trustworthy signal is behavior: a UIFail-guarded data read that returns
    // errSecInteractionNotAllowed proves the item's ACL distrusts us; anything
    // else never touches writes.

    private enum SilentReadOutcome {
        case ok(String)      // readable silently; token populated
        case notFound        // no item for this exact service+account
        case unreadable      // exists but ACL demands user interaction (poisoned)
    }

    // Single guarded data read (kSecUseAuthenticationUIFail): CANNOT spawn a
    // SecurityAgent dialog — macOS fails fast with errSecInteractionNotAllowed.
    private static func silentRead(account: String, service svc: String) -> SilentReadOutcome {
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
        if status == errSecSuccess, let data = item as? Data,
           let token = String(data: data, encoding: .utf8), !token.isEmpty {
            return .ok(token)
        }
        if status == errSecItemNotFound {
            return .notFound
        }
        NSLog("[EV-CRED] silent keychain read blocked status=\(status) service=\(svc)")
        return .unreadable
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

    // MARK: - One-time self-heal (exactly one guarded data read; never prompts)

    private static func healIfNeeded(for deviceID: String) {
        cacheLock.lock()
        let already = healDone
        healDone = true
        cacheLock.unlock()
        if already { return }

        let acct = account(for: deviceID)
        // Historical service names: never read; if any item lingers, drop it
        // silently (metadata query only decides existence).
        for legacy in [legacyService, legacyService2] where metadata(account: acct, service: legacy) != nil {
            NSLog("[EV-CRED] removing legacy keychain item service=\(legacy)")
            _ = cliDelete(account: acct, service: legacy)
        }
        switch silentRead(account: acct, service: service) {
        case .ok(let token):
            // Healthy item created by EV.app (default ACL trusts our stable
            // designated requirement). Cache and keep the 0600 mirror fresh;
            // NO keychain writes in steady state.
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            if loadFromFile(for: deviceID) == nil {
                saveToFile(token: token, for: deviceID)
            }
        case .notFound:
            // Canonical item absent: provision ONCE from the mirror so the
            // credential stays durably keychain-backed. Created by EV.app
            // itself → silent for all future launches and rebuilds.
            if let token = loadFromFile(for: deviceID) {
                NSLog("[EV-CRED] provisioning clean canonical keychain item for \(acct)")
                _ = save(token: token, for: deviceID)
            } else {
                NSLog("[EV-CRED] canonical keychain item missing and no local mirror; credential must be re-paired")
            }
        case .unreadable:
            // Item exists but its ACL demands user interaction — a foreign/
            // poisoned artifact. Reading it is forbidden (it would prompt);
            // replace it one time via security CLI delete + EV.app re-add from
            // the mirror. Zero user interaction throughout.
            guard let token = loadFromFile(for: deviceID) else {
                NSLog("[EV-CRED] unreadable foreign-ACL item has no local mirror; credential must be re-paired")
                return
            }
            NSLog("[EV-CRED] replacing unreadable (foreign ACL) keychain item for \(acct)")
            SecItemDelete([
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: acct,
            ] as CFDictionary)
            if metadata(account: acct, service: service) != nil {
                _ = cliDelete(account: acct, service: service)
            }
            _ = save(token: token, for: deviceID)
        }
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
        // K3 PRIMARY: 0600 mirror is the hot path — silent on every launch.
        // Never block AppConfig / menu-bar boot on SecItemAdd or
        // `/usr/bin/security` (those can sleep the process for tens of
        // seconds with 0% CPU and no ST00). Return the file token and
        // provision Keychain off the calling thread.
        if let token = loadFromFile(for: deviceID) {
            cacheLock.lock()
            cache[acct] = token
            cacheLock.unlock()
            DispatchQueue.global(qos: .utility).async {
                healIfNeeded(for: deviceID)
            }
            return token
        }
        // Mirror missing: one-time heal may read Keychain (UIFail, no prompt)
        // and is the only remaining credential source.
        healIfNeeded(for: deviceID)
        cacheLock.lock()
        let healed = cache[acct]
        cacheLock.unlock()
        return healed
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
