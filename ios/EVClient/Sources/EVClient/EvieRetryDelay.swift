// Cycle EAC-46 — iPhone offline retry delay.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieRetryDelay: Sendable {
    public static func seconds(attempt: Int) -> Int {
        let a = max(0, attempt)
        return min(300, [5, 15, 60, 180][min(a, 3)])
    }
}
