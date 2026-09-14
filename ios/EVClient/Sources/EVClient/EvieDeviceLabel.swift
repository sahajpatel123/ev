// Cycle EAC-47 — iPhone device label.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieDeviceLabel: Sendable {
    public static func shortName(model: String) -> String {
        let m = model.lowercased()
        if m.contains("16") && m.contains("pro") { return "16 Pro" }
        if m.contains("se") { return "SE" }
        return model
    }
}
