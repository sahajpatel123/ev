import Foundation

// =============================================================================
// DO NOT CHANGE — Mac API auth key resolution (locked)
// Other agents MUST NOT edit this file unless a live 401 is reproduced with
// a new root cause. EV.app 401s as "Invalid or revoked device token" when it
// sends a short leftover (EV_EARS_API_KEY / "changeme" / "dev") or a stale
// UserDefaults token instead of EV_MASTER_KEY.
//
// Rules (do not invert):
// 1. A usable key is >= 16 characters and not a known placeholder.
// 2. Prefer EV_MASTER_KEY from disk over EV_API_KEY and over UserDefaults.
// 3. Never persist an unusable key to UserDefaults.
// =============================================================================

public enum APIAuthKey {
    public static let minimumLength = 16

    public static func isUsable(_ value: String) -> Bool {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return false }
        switch trimmed.lowercased() {
        case "dev", "changeme", "secret", "placeholder", "your-key-here":
            return false
        default:
            return trimmed.count >= minimumLength
        }
    }

    public static func resolve(
        environment: [String: String],
        fileValues: [String: String],
        defaultsKey: String?
    ) -> String {
        let candidates = [
            environment["EV_API_KEY"],
            environment["EV_MASTER_KEY"],
            fileValues["EV_MASTER_KEY"],
            fileValues["EV_API_KEY"],
            defaultsKey,
        ]
        return candidates.compactMap { $0 }.first(where: { isUsable($0) }) ?? "dev"
    }
}
