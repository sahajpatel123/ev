// Cycle EAC-30 — iPhone permission explainer copy.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EviePermissionExplainer: Sendable {
    public static func consequence(for permission: String) -> String {
        switch permission.lowercased() {
        case "microphone": return "Talk and voice capture stay text-only"
        case "camera": return "Photo capture unavailable; share sheet still works"
        case "contacts": return "Name lookup unavailable; numbers still work"
        case "location": return "Nearby cards won't surface"
        case "notifications": return "Alerts stay in-app only"
        default: return "This feature stays off until granted"
        }
    }
}
