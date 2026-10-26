import Foundation

/// All animatable orb render parameters (AppKit-free).
///
/// `radius` / `thickness` are fractions of the view's half-size; `energy`,
/// `glow`, `halo`, `opacity`, `orbitStrength`, `bloom`, `coolDim`,
/// `confirmPulse` are 0...1; `turbulence` is 0...1; `flowSpeed` and
/// `rotationSpeed` are in revolutions per second at 1.0 (scaled in-shader).
/// `tint` multiplies the wisp color. Accumulated rotation lives in the state
/// machine (`rotationAngle`), not here, so it never trips snap detection.
public struct OrbParameters: Sendable, Equatable {
    public var energy: Float = 0
    public var radius: Float = 0.62
    public var thickness: Float = 0.075
    public var turbulence: Float = 0.45
    public var flowSpeed: Float = 0.2
    public var rotationSpeed: Float = 0.05
    public var glow: Float = 0.5
    public var halo: Float = 0
    public var opacity: Float = 0.92
    public var orbitStrength: Float = 0
    public var bloom: Float = 0
    public var coolDim: Float = 0
    public var confirmPulse: Float = 0
    public var tintR: Float = 1
    public var tintG: Float = 1
    public var tintB: Float = 1

    public init() {}

    /// Subtle accent tint for `.needsConfirm` (configurable per spec).
    public static var accentTint: (r: Float, g: Float, b: Float) = (0.72, 0.88, 1.0)

    /// Scalar fields driven by the critically damped spring (tint lerps
    /// exponentially instead, to avoid overshoot past gamut edges).
    static let springFields: [WritableKeyPath<OrbParameters, Float>] = [
        \.energy, \.radius, \.thickness, \.turbulence, \.flowSpeed,
        \.rotationSpeed, \.glow, \.halo, \.opacity, \.orbitStrength,
        \.bloom, \.coolDim, \.confirmPulse,
    ]

    /// Target parameters for a state. `level` is the smoothed 0...1 audio
    /// envelope routed to this state (input for listening, output for
    /// speaking, ignored otherwise).
    public static func target(
        for state: OrbState,
        level: Float,
        reduceMotion: Bool,
        appearance: OrbAppearance
    ) -> OrbParameters {
        var p = OrbParameters()
        let l = min(1, max(0, level))
        switch state {
        case .idle:
            p.energy = 0.06
            p.flowSpeed = 0.2
            p.rotationSpeed = 0.05
            p.glow = 0.5
        case .listening:
            p.energy = 0.15 + 0.85 * l
            p.thickness = 0.08 + 0.05 * l
            p.turbulence = 0.6
            p.flowSpeed = 0.9
            p.rotationSpeed = 0.1
            p.glow = 0.7 + 0.4 * l
        case .thinking:
            p.energy = 0.25
            p.radius = 0.58
            p.thickness = 0.07
            p.turbulence = 0.5
            p.flowSpeed = 0.35
            p.rotationSpeed = 0.35
            p.glow = 0.55
        case .acting:
            p.energy = 0.3
            p.radius = 0.60
            p.thickness = 0.075
            p.turbulence = 0.5
            p.flowSpeed = 0.5
            p.rotationSpeed = 0.15
            p.glow = 0.6
            p.orbitStrength = 0.8
        case .speaking:
            p.energy = 0.15 + 0.85 * l
            p.thickness = 0.08 + 0.05 * l
            p.turbulence = 0.6
            p.flowSpeed = 1.0
            p.rotationSpeed = 0.1
            p.glow = 0.7 + 0.4 * l
        case .needsConfirm:
            p.energy = 0.3
            p.radius = 0.60
            p.flowSpeed = 0.3
            p.rotationSpeed = 0.08
            p.glow = 0.6
            p.confirmPulse = 1.0
            p.tintR = accentTint.r
            p.tintG = accentTint.g
            p.tintB = accentTint.b
        case .success:
            p.energy = 0.4
            p.flowSpeed = 0.6
            p.glow = 1.2
            p.bloom = 1.0
        case .error:
            p.energy = 0.1
            p.flowSpeed = 0.15
            p.rotationSpeed = 0.03
            p.glow = 0.25
            p.coolDim = 1.0
            p.tintR = 0.70
            p.tintG = 0.80
            p.tintB = 0.95
        case .offline:
            p.energy = 0.03
            p.thickness = 0.03
            p.turbulence = 0.15
            p.flowSpeed = 0
            p.rotationSpeed = 0
            p.glow = 0.2
            p.opacity = 0.35
        }
        if reduceMotion {
            p.flowSpeed = 0
            p.rotationSpeed = 0
            p.orbitStrength = 0
            p.turbulence = min(p.turbulence, 0.3)
        }
        p.halo = (appearance == .light) ? 0.4 : 0
        return p
    }

    /// One critically damped spring step toward `target` (semi-implicit
    /// Euler, damping ratio = 1). Snaps when close so static states rest
    /// at exactly their targets.
    public mutating func approach(
        target: OrbParameters,
        velocity: inout OrbParameters,
        dt: TimeInterval,
        stiffness: Float
    ) {
        let clamped = min(max(dt, 0), 0.1)
        let c = 2 * sqrt(stiffness)
        for field in Self.springFields {
            let x = self[keyPath: field]
            let t = target[keyPath: field]
            var v = velocity[keyPath: field]
            let acc = stiffness * (t - x) - c * v
            v += acc * Float(clamped)
            var nx = x + v * Float(clamped)
            if abs(t - nx) < 1e-3 && abs(v) < 1e-2 {
                nx = t
                v = 0
            }
            self[keyPath: field] = nx
            velocity[keyPath: field] = v
        }
        // Tint: exponential approach, no overshoot.
        let alpha = Float(1 - exp(-clamped * 8))
        tintR += (target.tintR - tintR) * alpha
        tintG += (target.tintG - tintG) * alpha
        tintB += (target.tintB - tintB) * alpha
        if abs(target.tintR - tintR) < 1e-3 { tintR = target.tintR }
        if abs(target.tintG - tintG) < 1e-3 { tintG = target.tintG }
        if abs(target.tintB - tintB) < 1e-3 { tintB = target.tintB }
    }

    /// Per-field absolute differences (tint as three entries).
    public static func diffMagnitude(_ a: OrbParameters, _ b: OrbParameters) -> [Float] {
        var out = springFields.map { abs(a[keyPath: $0] - b[keyPath: $0]) }
        out.append(abs(a.tintR - b.tintR))
        out.append(abs(a.tintG - b.tintG))
        out.append(abs(a.tintB - b.tintB))
        return out
    }
}
