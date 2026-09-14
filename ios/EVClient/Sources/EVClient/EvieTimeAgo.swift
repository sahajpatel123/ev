// Cycle EAC-28 — iPhone relative-time formatter.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieTimeAgo: Sendable {
    public static func label(since seconds: Int) -> String {
        let s = max(0, seconds)
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60)m ago" }
        if s < 86400 { return "\(s / 3600)h ago" }
        if s < 604800 { return "\(s / 86400)d ago" }
        return "\(s / 604800)w ago"
    }
}
