import Foundation

/// The orb's visual states. Driven by presence events via ``PresenceMapping``;
/// momentary states (`.success`, `.error`) decay back to `.idle` on their own.
public enum OrbState: String, CaseIterable, Sendable {
    case idle
    case listening
    case thinking
    case acting
    case speaking
    case needsConfirm
    case success
    case error
    case offline
}

/// Backdrop appearance the orb composites over.
public enum OrbAppearance: Sendable {
    case dark
    case light
}
