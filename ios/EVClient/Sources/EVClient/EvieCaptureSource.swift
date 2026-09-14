// Cycle EAC-52 — iPhone capture-source catalog.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Every way a moment enters Evie on either iPhone (16 Pro + SE): voice, camera,
/// share sheet, typed note, watch quick-capture, Siri shortcut, or file.
/// One enum gives Capture, timeline filters, and the offline queue the same
/// labels and SF Symbol names, so both phones render identical source rows.
/// Strings only — Foundation alone, CLT compiles. No backend change.

import Foundation

public enum EvieCaptureSource: String, Sendable, Equatable, CaseIterable {
    case voice
    case camera
    case share
    case notes
    case watch
    case shortcut
    case file

    /// Short label for capture rows and queue badges.
    public func label() -> String {
        switch self {
        case .voice:
            return "Voice"
        case .camera:
            return "Camera"
        case .share:
            return "Share"
        case .notes:
            return "Note"
        case .watch:
            return "Watch"
        case .shortcut:
            return "Shortcut"
        case .file:
            return "File"
        }
    }

    /// SF Symbol name the Xcode-only UI renders; never loaded here.
    public func iconName() -> String {
        switch self {
        case .voice:
            return "mic.fill"
        case .camera:
            return "camera.fill"
        case .share:
            return "square.and.arrow.up"
        case .notes:
            return "square.and.pencil"
        case .watch:
            return "applewatch"
        case .shortcut:
            return "bolt.fill"
        case .file:
            return "doc.fill"
        }
    }

    /// Sources that need no extra permission prompt beyond the app itself.
    public var isPermissionFree: Bool {
        self == .notes || self == .shortcut
    }
}
