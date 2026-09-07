import Foundation

public enum TrustedOrigin {
    public static func allows(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        if host == "localhost" || host == "127.0.0.1" { return true }
        if host.hasSuffix(".ts.net") { return true }
        return false
    }
}

// Cycle 50 — iPhone-only, backward compat: bridge-security pinning notes (comment-only).
// No behavior change; documents intended transport-pinning posture for the shell.
// - Pinning scope: device-gateway origin only (AppOrigin.apiOrigin); web-view content
//   origins remain governed by TrustedOrigin.allows(_:).
// - Rotation: overlap old + new pins during rollout; fail-closed only after both ship.
// - iPhone-only: performed in the native shell; the web core never sees pins or keys.
