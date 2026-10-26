import Accelerate
import Foundation

/// Audio levels the orb breathes with. Fed from PCM buffers the app already
/// holds (mic capture tap, TTS playback tap) — never from a separate capture.
///
/// Threading: ingest runs on the audio thread (one RMS per buffer max);
/// `inputLevel` / `outputLevel` are single lock-guarded Floats the renderer
/// reads once per frame.
public protocol OrbLevelSource: AnyObject, Sendable {
    var inputLevel: Float { get }
    var outputLevel: Float { get }
}

/// vDSP level math for 16 kHz mono PCM16 (host-endian, matching MicCapture).
public enum OrbLevelDSP {
    /// Int16 RMS treated as silence (matches MicCapture.isQuiet).
    public static let silenceFloor: Float = 80
    /// Int16 RMS of typical close-talk speech after smoothing.
    public static let speechReference: Float = 3_600

    /// RMS of little-endian mono int16 samples via Accelerate.
    public static func rmsPCM16(_ data: Data) -> Float {
        let count = data.count / 2
        guard count > 0 else { return 0 }
        var floats = [Float](repeating: 0, count: count)
        var rms: Float = 0
        data.withUnsafeBytes { raw in
            guard let samples = raw.baseAddress?.assumingMemoryBound(to: Int16.self) else { return }
            floats.withUnsafeMutableBufferPointer { out in
                guard let dst = out.baseAddress else { return }
                vDSP_vflt16(samples, 1, dst, 1, vDSP_Length(count))
                vDSP_rmsqv(dst, 1, &rms, vDSP_Length(count))
            }
        }
        return rms
    }

    /// Map raw RMS onto 0...1: hard gate at the silence floor, gentle power
    /// curve so quiet speech still moves the orb.
    public static func normalize(rms: Float) -> Float {
        guard rms > silenceFloor else { return 0 }
        let span = max(speechReference - silenceFloor, 1)
        let linear = min(1, (rms - silenceFloor) / span)
        return pow(linear, 0.55)
    }
}

/// The live level source. Call `ingestInputPCM16` from the existing mic tap
/// and `ingestOutputPCM16` from the existing TTS PCM path (see ORB_RECON §1).
public final class AtomicOrbLevelMeter: OrbLevelSource, @unchecked Sendable {
    public static let shared = AtomicOrbLevelMeter()

    private let lock = NSLock()
    private var input: Float = 0
    private var output: Float = 0
    private var lastInputAt = Date.distantPast
    private var lastOutputAt = Date.distantPast

    /// Release time constant applied between ingests (seconds).
    private let releaseTau: TimeInterval

    public init(releaseTau: TimeInterval = 0.22) {
        self.releaseTau = releaseTau
    }

    public var inputLevel: Float {
        lock.lock()
        defer { lock.unlock() }
        return input
    }

    public var outputLevel: Float {
        lock.lock()
        defer { lock.unlock() }
        return output
    }

    public func reset() {
        lock.lock()
        input = 0
        output = 0
        lastInputAt = Date.distantPast
        lastOutputAt = Date.distantPast
        lock.unlock()
    }

    /// One RMS computation per call; safe on the audio thread.
    public func ingestInputPCM16(_ data: Data) {
        ingest(OrbLevelDSP.normalize(rms: OrbLevelDSP.rmsPCM16(data)), input: true)
    }

    /// One RMS computation per call; safe on the audio thread.
    public func ingestOutputPCM16(_ data: Data) {
        ingest(OrbLevelDSP.normalize(rms: OrbLevelDSP.rmsPCM16(data)), input: false)
    }

    private func ingest(_ sample: Float, input isInput: Bool) {
        lock.lock()
        defer { lock.unlock() }
        let now = Date()
        if isInput {
            let dt = max(now.timeIntervalSince(lastInputAt), 0.001)
            lastInputAt = now
            // Fast attack toward louder, exponential release toward quiet.
            let alpha = sample >= input ? Float(0.7) : Float(1 - exp(-dt / releaseTau))
            input += (sample - input) * alpha
            if input < 0.005 && sample == 0 { input = 0 }
        } else {
            let dt = max(now.timeIntervalSince(lastOutputAt), 0.001)
            lastOutputAt = now
            let alpha = sample >= output ? Float(0.7) : Float(1 - exp(-dt / releaseTau))
            output += (sample - output) * alpha
            if output < 0.005 && sample == 0 { output = 0 }
        }
    }
}

/// Deterministic speech-like level source for checks and previews (no audio).
public final class SyntheticLevelSource: OrbLevelSource, @unchecked Sendable {
    public enum Profile: Sendable {
        /// Owner talking: input breathes in syllables with pauses.
        case speech
        /// Evie talking: output breathes, input stays quiet.
        case playback
        /// Silence with a whisper of room tone below the gate.
        case quiet
    }

    private let lock = NSLock()
    private var t: TimeInterval = 0
    private let profile: Profile
    private let seedPhase: Double

    public init(seed: UInt64 = 1, profile: Profile = .speech) {
        self.profile = profile
        // Deterministic per-seed phase offset (golden-ratio scramble).
        self.seedPhase = Double(seed &* 0x9E3779B97F4A7C15 % 1000) / 1000 * 2 * .pi
    }

    public var inputLevel: Float {
        lock.lock()
        defer { lock.unlock() }
        return level(at: t, channel: .input)
    }

    public var outputLevel: Float {
        lock.lock()
        defer { lock.unlock() }
        return level(at: t, channel: .output)
    }

    /// Advance the synthetic clock (call once per rendered frame).
    public func advance(by dt: TimeInterval) {
        lock.lock()
        t += dt
        lock.unlock()
    }

    private enum Channel { case input, output }

    private func level(at time: TimeInterval, channel: Channel) -> Float {
        switch (profile, channel) {
        case (.quiet, _):
            return 0.005
        case (.speech, .output), (.playback, .input):
            return 0.005
        case (.speech, .input), (.playback, .output):
            // ~3.5 Hz syllables, 0.45 s pause every 1.6 s, deterministic wobble.
            let tt = time + seedPhase
            let syllable = max(0, sin(2 * .pi * 3.5 * tt))
            let pauseGate = (tt.truncatingRemainder(dividingBy: 1.6) > 1.15) ? 0.0 : 1.0
            let wobble = 0.85 + 0.15 * sin(2 * .pi * 0.9 * tt + 1.3)
            return Float(syllable * pauseGate * wobble * 0.9)
        }
    }
}
