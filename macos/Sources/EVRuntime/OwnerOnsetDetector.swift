import Foundation

/// Interrupt V2 owner-onset detector (NEW architecture, owner-ordered).
///
/// This is not the closed V1 monitor: no mel templates, no analysis side
/// channel, no calibration files. It polls the already-smoothed
/// `VoiceLevelMeter` levels on the main actor (zero audio-thread work) and
/// confirms owner speech during Eve's playback by level-differential plus
/// persistence:
///
/// - onset requires mic clearly ABOVE playback bleed
///   (`input >= max(threshold, output * marginRatio + marginFloor)`),
/// - 3 consecutive 100 ms polls (300 ms) must all hit,
/// - the output meter must have registered playback energy at least once
///   since the episode began (warmup guard). At reply onset the mic hears
///   speaker bleed instantly while the output meter still reads ~0, so
///   without this the differential compares hot bleed against a zero
///   baseline and every reply self-interrupts. Until warmup no poll may
///   accumulate toward the streak.
/// - confidence is this detector's own score: base 0.65 for a completed
///   persistence window, scaled up by margin excess. Documented semantics —
///   the server applies the higher no-AEC bar (0.6) on top.
///
/// The server is the backstop: fused evidence can still land AMBIGUOUS
/// (short speech, echo self-report, latch duplicate) and then nothing
/// interrupts. Honest gaps: no AEC on this path (`aec_active: false` is
/// reported truthfully) and no measured echo correlation.
public struct OwnerOnsetDetector {
    public struct Config {
        public var threshold: Float = 0.30
        // The correlation veto (not this differential) is the bleed
        // discriminator: it compares content, so the level bar only needs
        // to reject input at/below the output energy. Measured live
        // 2026-10-08 against the render-fed meter: bleed peaks at 0.43
        // while the output reads 0.28-0.93, so 1.5 and 1.1 both silenced
        // the detector outright (zero onsets across 40 s of bleed).
        public var marginRatio: Float = 1.0
        public var marginFloor: Float = 0.10
        public var pollsToConfirm: Int = 3
        public var pollIntervalMs: Int = 100
        public var cooldownSeconds: TimeInterval = 2.0
        public var outputWarmupFloor: Float = 0.05

        public init() {}
    }

    public struct Confirmation {
        public var speechMs: Int
        public var confidence: Double
    }

    private let config: Config
    private var streak = 0
    private var onsetStart: Date?
    private var cooldownUntil = Date.distantPast
    private var outputWarmedUp = false

    public init(config: Config = Config()) {
        self.config = config
    }

    public mutating func reset() {
        streak = 0
        onsetStart = nil
        outputWarmedUp = false
    }

    /// One 100 ms poll. Returns a confirmation exactly once per onset.
    public mutating func poll(input: Float, output: Float, echoGate: Bool, now: Date) -> Confirmation? {
        guard echoGate else {
            reset()
            return nil
        }
        if output >= config.outputWarmupFloor {
            outputWarmedUp = true
        }
        guard now >= cooldownUntil else { return nil }
        guard outputWarmedUp else { return nil }
        let required = max(config.threshold, output * config.marginRatio + config.marginFloor)
        guard input >= required else {
            reset()
            return nil
        }
        streak += 1
        if onsetStart == nil { onsetStart = now }
        guard streak >= config.pollsToConfirm else { return nil }
        let elapsedMs: Int
        if let start = onsetStart {
            elapsedMs = max(config.pollsToConfirm * config.pollIntervalMs, Int(now.timeIntervalSince(start) * 1000))
        } else {
            elapsedMs = config.pollsToConfirm * config.pollIntervalMs
        }
        let excess = input - required
        let confidence = Double(min(1.0, 0.65 + excess * 1.2))
        cooldownUntil = now.addingTimeInterval(config.cooldownSeconds)
        reset()
        return Confirmation(speechMs: elapsedMs, confidence: confidence)
    }
}

/// Bounded mic preroll ring (16 kHz mono PCM16, 32 bytes/ms).
///
/// Fed from the capture tap while the half-duplex gate holds mic forwarding
/// during Eve's playback; flushed to the provider on a confirmed cut-in so
/// the utterance onset is not lost. Lock-guarded for tap-thread safety.
public final class OnsetPrerollRing: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private let capBytes: Int

    public init(capBytes: Int = 51_200) {
        self.capBytes = capBytes
    }

    public func append(_ pcm: Data) {
        guard !pcm.isEmpty else { return }
        lock.lock()
        data.append(pcm)
        if data.count > capBytes {
            data.removeFirst(data.count - capBytes)
        }
        lock.unlock()
    }

    public func flush() -> Data {
        lock.lock()
        defer { lock.unlock() }
        let out = data
        data.removeAll(keepingCapacity: true)
        return out
    }

    /// Non-destructive peek at the most recent buffered audio (for the
    /// correlation veto, which must not consume the preroll evidence).
    public func recent(maxBytes: Int) -> Data {
        lock.lock()
        defer { lock.unlock() }
        guard maxBytes > 0, !data.isEmpty else { return Data() }
        if data.count <= maxBytes { return data }
        // Normalize to zero-based: Data.suffix preserves the source
        // startIndex, and integer-subscript consumers trap on such slices.
        return Data(data.suffix(maxBytes))
    }

    public var bufferedMs: Int {
        lock.lock()
        defer { lock.unlock() }
        return data.count / 32
    }
}
