// Cycle EAC-24 — iPhone data-saver upload policy.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Decides what the offline queue on both iPhones (16 Pro + SE) may upload
/// while the owner is on a metered / Low Data Mode connection: text captures
/// always sync, photo/file attachments wait for unmetered Wi-Fi. Pure policy
/// values — the Xcode-only queue driver reads `allowsUpload(kind:isMetered:)`;
/// Foundation only so CLT compiles. No backend change.

import Foundation

public struct EvieDataSaver: Equatable, Sendable {
    public enum PayloadKind: String, Sendable, Equatable {
        case text
        case photo
        case file
        case audio
    }

    public var enabled: Bool

    public init(enabled: Bool = true) {
        self.enabled = enabled
    }

    /// True when a payload of this kind may upload right now.
    /// Text always syncs (tiny); anything with bytes waits on metered links
    /// while data-saver is enabled.
    public func allowsUpload(kind: PayloadKind, isMetered: Bool) -> Bool {
        guard enabled, isMetered else { return true }
        return kind == .text
    }

    /// One line for settings copy. Example: "data saver on · photos wait for Wi-Fi".
    public func summaryLine() -> String {
        enabled ? "data saver on · photos wait for Wi-Fi" : "data saver off"
    }
}
