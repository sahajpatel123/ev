import Foundation

/// Owns the current ``OrbState`` and interpolates ``OrbParameters`` toward
/// each state's target with a critically damped spring (no snapping).
///
/// Level routing: `.listening` breathes from the input envelope,
/// `.speaking` from the output envelope; all other states use fixed energy.
/// `.success` / `.error` are momentary and decay back to `.idle`.
public final class OrbStateMachine: @unchecked Sendable {
    /// How long the success bloom plays before settling to idle.
    public var successDuration: TimeInterval = 1.4
    /// How long the error dim holds before settling to idle.
    public var errorDuration: TimeInterval = 1.1

    private let stiffness: Float
    private let reduceMotion: Bool
    private let appearance: OrbAppearance
    private let lock = NSLock()
    private var _state: OrbState = .idle
    private var params = OrbParameters()
    private var velocity = OrbParameters()
    private var inputEnvelope = EnvelopeFollower()
    private var outputEnvelope = EnvelopeFollower()
    private var momentaryRemaining: TimeInterval? = nil
    private var _rotationAngle: Float = 0

    public init(
        stiffness: Float = 90,
        reduceMotion: Bool = false,
        appearance: OrbAppearance = .dark
    ) {
        self.stiffness = stiffness
        self.reduceMotion = reduceMotion
        self.appearance = appearance
        // Start settled at the idle target so the first frame is valid.
        params = OrbParameters.target(for: .idle, level: 0, reduceMotion: reduceMotion, appearance: appearance)
    }

    public var state: OrbState {
        lock.lock()
        defer { lock.unlock() }
        return _state
    }

    public var current: OrbParameters {
        lock.lock()
        defer { lock.unlock() }
        return params
    }

    /// Accumulated ring rotation in radians (frozen under Reduce Motion).
    public var rotationAngle: Float {
        lock.lock()
        defer { lock.unlock() }
        return _rotationAngle
    }

    public func setState(_ newState: OrbState) {
        lock.lock()
        _state = newState
        switch newState {
        case .success:
            momentaryRemaining = successDuration
        case .error:
            momentaryRemaining = errorDuration
        default:
            momentaryRemaining = nil
        }
        lock.unlock()
    }

    /// Advance one frame; returns the interpolated parameters.
    @discardableResult
    public func update(dt: TimeInterval, inputLevel: Float, outputLevel: Float) -> OrbParameters {
        lock.lock()
        if let remaining = momentaryRemaining {
            let next = remaining - dt
            if next <= 0 {
                _state = .idle
                momentaryRemaining = nil
            } else {
                momentaryRemaining = next
            }
        }
        let inEnv = inputEnvelope.update(sample: inputLevel, dt: dt)
        let outEnv = outputEnvelope.update(sample: outputLevel, dt: dt)
        let level: Float
        switch _state {
        case .listening: level = inEnv
        case .speaking: level = outEnv
        default: level = 0
        }
        let target = OrbParameters.target(
            for: _state, level: level, reduceMotion: reduceMotion, appearance: appearance
        )
        params.approach(target: target, velocity: &velocity, dt: dt, stiffness: stiffness)
        _rotationAngle += params.rotationSpeed * Float(dt) * 2 * .pi * 0.25
        let out = params
        lock.unlock()
        return out
    }
}
