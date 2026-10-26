import Foundation

/// Asymmetric envelope follower: fast attack, slow release, perceptual scaling.
///
/// Input is a linear 0...1 audio level (typically from ``OrbLevelDSP``).
/// Processing order: clamp -> noise gate -> log (perceptual) curve ->
/// one-pole attack/release smoothing. The gate keeps background hum from
/// moving the orb; the log curve keeps quiet speech visible.
public struct EnvelopeFollower: Sendable {
    /// Attack time constant in seconds (default 30 ms).
    public var attackTau: TimeInterval
    /// Release time constant in seconds (default 250 ms).
    public var releaseTau: TimeInterval
    /// Linear inputs below this map to zero (default 0.02).
    public var gateThreshold: Float
    /// Steepness of the log curve `log(1 + k*x) / log(1 + k)` (default 9).
    public var logCurve: Float

    private var value: Float = 0

    public init(
        attackTau: TimeInterval = 0.030,
        releaseTau: TimeInterval = 0.250,
        gateThreshold: Float = 0.02,
        logCurve: Float = 9
    ) {
        self.attackTau = attackTau
        self.releaseTau = releaseTau
        self.gateThreshold = gateThreshold
        self.logCurve = logCurve
    }

    public var current: Float { value }

    public mutating func reset() {
        value = 0
    }

    /// Advance by `dt` seconds with a new linear sample; returns the envelope.
    @discardableResult
    public mutating func update(sample: Float, dt: TimeInterval) -> Float {
        let clamped = min(1, max(0, sample))
        let gated: Float
        if clamped < gateThreshold {
            gated = 0
        } else {
            gated = (clamped - gateThreshold) / (1 - gateThreshold)
        }
        let perceptual = log(1 + logCurve * gated) / log(1 + logCurve)
        let tau = perceptual >= value ? attackTau : releaseTau
        let alpha = Float(1 - exp(-dt / max(tau, 1e-6)))
        value += (perceptual - value) * alpha
        if value < 1e-4 && perceptual == 0 { value = 0 }
        return value
    }
}
