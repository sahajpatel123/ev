import Foundation

public enum TrustedOrigin {
    public static func allows(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        if host == "localhost" || host == "127.0.0.1" { return true }
        if host.hasSuffix(".ts.net") { return true }
        return false
    }
}
