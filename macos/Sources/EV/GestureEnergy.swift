import Foundation

/// Server gesture vocabulary, mirrored from `backend/app/voice/live/gesture.py`.
/// Unknown values map to `.none` so future gestures never break the orb.
enum LiveGesture: String {
    case none = ""
    case idle
    case listening
    case thinking
    case speaking
    case yielding
    case yieldBack = "yield_back"
    case contested
    case urgent

    init(raw: String?) {
        guard let raw, let known = LiveGesture(rawValue: raw) else {
            self = .none
            return
        }
        self = known
    }
}

/// Gesture modulation for the speech-response orb.
///
/// Frozen-safe by construction: this only scales the audio level that the
/// existing renderer already consumes. No new statuses, no new visuals, no
/// change to the still/video composition — presence reads as energy.
enum GestureEnergy {
    /// Scale `base` (0...1 output level) for the current gesture.
    static func modulate(base: Float, gesture: LiveGesture, intensity: String?, time: TimeInterval) -> Float {
        let level = max(0, min(1, base))
        switch gesture {
        case .none, .idle, .listening, .speaking:
            return level
        case .thinking:
            // Held, low simmer while Eve composes.
            return max(level * 0.5, 0.12)
        case .yielding:
            // Cut off: energy dips as Eve gives the floor up.
            return level * 0.35
        case .yieldBack:
            // "Sorry — go on": soft return, slightly under speaking level.
            return level * 0.6
        case .contested:
            // Overlap: gentle pulse so neither side reads as dropped.
            let pulse = 0.5 + 0.5 * Float(sin(time * 6.0))
            return max(level * 0.55, 0.15 + 0.25 * pulse)
        case .urgent:
            // Safety/timer cut-in: full presence, never dimmed.
            let boost: Float = (intensity == "high") ? 1.0 : 0.85
            return max(level, boost)
        }
    }
}
