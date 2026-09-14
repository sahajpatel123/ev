// Cycle EAC-49 — iPhone API error copy.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieAPIErrorCopy: Sendable {
    public static func line(statusCode: Int) -> String {
        switch statusCode {
        case 401: return "Sign in again on this iPhone"
        case 409: return "Already saved — nothing lost"
        case 422: return "Couldn't save — kept for review"
        case 500...599: return "Server hiccup — queued to retry"
        default: return "Couldn't reach EV — queued offline"
        }
    }
}
