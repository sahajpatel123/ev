// Cycle EAC-48 — iPhone keychain account key.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieKeychainKey: Sendable {
    public static func account(deviceId: String) -> String {
        "ev-token-\(deviceId.trimmingCharacters(in: .whitespacesAndNewlines))"
    }
}
