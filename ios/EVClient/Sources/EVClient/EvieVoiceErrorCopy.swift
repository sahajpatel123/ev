// Cycle EAC-29 — iPhone voice error copy.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieVoiceErrorCopy: Sendable {
    public static func line(for code: String) -> String {
        switch code {
        case "asr_empty_result": return "Didn't catch that — try again"
        case "asr_auth_failed": return "Voice check failed — type instead"
        case "listening_stopped": return "Live session ended"
        default: return "Voice hiccup — reconnecting"
        }
    }
}
