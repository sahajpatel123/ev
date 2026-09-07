// Cycle EAC-20 — iPhone mic level display mapping.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Maps a measured PCM RMS value (0.0–1.0, 16 kHz mono path shared by both
/// iPhones) to a display-only 0.0–1.0 meter level with a noise floor and
/// ceiling. Display shaping only — not a calibrated loudness measurement.
/// Takes numbers, never audio buffers, so no AVFoundation import and CLT
/// still compiles. No backend change.

import Foundation

public enum EvieMicLevel: Sendable {
    /// Noise floor below which the meter reads zero (room tone on a phone mic).
    public static let noiseFloor: Float = 0.02

    /// Display level for a measured RMS. Clamps input to 0...1, subtracts the
    /// noise floor, then applies a gentle square-root curve so quiet speech
    /// still moves the meter.
    public static func level(rms: Float) -> Float {
        let clamped = max(0, min(1, rms))
        guard clamped > noiseFloor else { return 0 }
        let shaped = (clamped - noiseFloor) / (1 - noiseFloor)
        return max(0, min(1, shaped.squareRoot()))
    }

    /// True when the meter should show the "hearing you" state.
    public static func isAudible(rms: Float) -> Bool {
        rms > noiseFloor
    }
}
