// Cycle EAC-27 — iPhone offline queue badge accessibility label.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieQueueBadge: Sendable {
    public static func label(pendingCount: Int) -> String {
        let n = max(0, pendingCount)
        if n == 0 { return "Offline queue clear" }
        if n == 1 { return "1 offline capture pending" }
        return "\(n) offline captures pending"
    }
}
