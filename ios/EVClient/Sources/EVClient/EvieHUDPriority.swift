// Cycle EAC-38 — iPhone HUD priority mapper.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieHUDPriority: Sendable {
    public static func urgencyLabel(priority: Double) -> String {
        if priority >= 0.8 { return "urgent" }
        if priority >= 0.5 { return "notable" }
        if priority > 0 { return "quiet" }
        return "idle"
    }
}
