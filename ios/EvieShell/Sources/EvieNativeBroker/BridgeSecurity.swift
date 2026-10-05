import Foundation

public enum TrustedOrigin {
    /// iPhone mic rule (2026-10-03 fix): the page must be a secure context.
    /// `https://<mac>.ts.net/evie/` is the only voice-capable origin.
    /// Numeric tailnet/LAN IPs over http (`http://100.x:8000`) are allowed
    /// here so the shell can load diagnostics instead of white-screening,
    /// but WebKit `getUserMedia` still fails there (M01) by platform design.
    /// The PWA surfaces that as "open the https ts.net URL".
    public static func allows(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        if host == "localhost" || host == "127.0.0.1" || host == "::1" { return true }
        if host.hasSuffix(".ts.net") { return true }
        if isPrivateIP(host) { return true }
        return false
    }

    /// True only for origins where iOS grants microphone capture:
    /// https, or http loopback (treated secure). Everything else —
    /// including http://100.x tailnet IPs — fails M01 by design.
    public static func isVoiceCapable(_ url: URL) -> Bool {
        let scheme = (url.scheme ?? "").lowercased()
        guard let host = url.host?.lowercased() else { return false }
        if scheme == "https" { return allows(url) }
        return host == "localhost" || host == "127.0.0.1" || host == "::1"
    }

    static func isPrivateIP(_ host: String) -> Bool {
        let h = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if h.contains(":") {
            // IPv6 loopback / link-local / unique-local: diagnostics only.
            return h == "::1" || h.hasPrefix("fe80:") || h.hasPrefix("fc") || h.hasPrefix("fd")
        }
        let parts = h.split(separator: ".")
        guard parts.count == 4, parts.allSatisfy({ Int($0) != nil }) else { return false }
        let b = parts.map { Int($0)! }
        guard b.allSatisfy({ $0 >= 0 && $0 <= 255 }) else { return false }
        if b[0] == 10 { return true }                                        // 10/8
        if b[0] == 172 && (16...31).contains(b[1]) { return true }            // 172.16/12
        if b[0] == 192 && b[1] == 168 { return true }                         // 192.168/16
        if b[0] == 100 && (64...127).contains(b[1]) { return true }           // Tailscale CGNAT 100.64/10
        if b[0] == 127 { return true }                                        // 127/8
        if b[0] == 169 && b[1] == 254 { return true }                         // link-local
        return false
    }
}

// Cycle 50 — iPhone-only, backward compat: bridge-security pinning notes (comment-only).
// No behavior change; documents intended transport-pinning posture for the shell.
// - Pinning scope: device-gateway origin only (AppOrigin.apiOrigin); web-view content
//   origins remain governed by TrustedOrigin.allows(_:).
// - Rotation: overlap old + new pins during rollout; fail-closed only after both ship.
// - iPhone-only: performed in the native shell; the web core never sees pins or keys.
