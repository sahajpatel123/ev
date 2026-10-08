import Foundation

/// Reference-correlation bleed veto for Interrupt V2 spoken cut-in.
///
/// Level comparison cannot separate speaker bleed from owner speech without
/// AEC (measured in=0.82 out=0.17 on a stock MacBook — bleed 5x hotter than
/// the output meter). Correlation against the played reference PCM can: it
/// is amplitude-invariant, so room gain staging drops out.
///
/// Role: VETO ONLY. The level detector still decides "loud enough to be an
/// onset"; this answers "but is the mic just hearing ourselves?" A high
/// peak correlation means bleed → suppress the confirm. Owner alone and
/// double-talk pass through to the level verdict. Too-short windows veto
/// (the detector re-polls in 100 ms; a real cut-in only waits for data).
/// Silence splits by side: a quiet mic is a transient (veto), but a loud
/// mic over a quiet reference cannot be bleed — nothing audible to bleed
/// from — so it confirms (the owner cutting through a reply pause).
///
/// Measured on real speech (Samantha ref vs Daniel owner, 240 ms windows,
/// +/-50 ms lag, decimation 4): pure bleed 0.90-0.99 across attenuations,
/// owner alone 0.05, double-talk 0.17-0.55. The 0.65 veto threshold clears
/// all pure bleed with margin while letting even 1:1 double-talk through.
public struct ReferenceCorrelator {
    public struct Config {
        /// 16 kHz samples per analysis window (3840 = 240 ms).
        public var windowSamples: Int = 3840
        /// +/- search range in 16 kHz samples (800 = 50 ms room delay).
        public var maxLagSamples: Int = 800
        /// Keep every Nth sample (4 = 4 kHz effective; ~385k mults/poll).
        public var decimation: Int = 4
        /// Peak correlation at/above this vetoes the onset.
        public var vetoThreshold: Float = 0.65
        /// Absolute RMS floor (int16 units): windows quieter than this
        /// carry no information — never veto on them.
        public var energyFloor: Float = 300

        public init() {}
    }

    private let config: Config

    public init(config: Config = Config()) {
        self.config = config
    }

    /// Peak absolute normalized cross-correlation of the reference window
    /// against the microphone span. `microphone` must cover the window plus
    /// the full lag search on both sides. Returns 0 when either side is
    /// below the energy floor (nothing to judge).
    public func peakCorrelation(
        reference: [Int16], microphone: [Int16]
    ) -> Float {
        let decim = max(1, config.decimation)
        let ml = config.maxLagSamples / decim
        let need = config.windowSamples / decim
        guard reference.count >= need,
              microphone.count >= need + 2 * ml else { return 0 }
        let ref = strideSlice(reference, count: need, decim: decim)
        let mic = strideSlice(microphone, count: need + 2 * ml, decim: decim)
        let refRMS = rms(ref)
        guard refRMS >= config.energyFloor else { return 0 }
        var best: Float = 0
        var refEnergy: Float = 0
        for v in ref { refEnergy += v * v }
        guard refEnergy > 0 else { return 0 }
        for lag in -ml...ml {
            let base = ml + lag
            var dot: Float = 0
            var micEnergy: Float = 0
            for i in 0..<need {
                let m = mic[base + i]
                dot += ref[i] * m
                micEnergy += m * m
            }
            guard micEnergy > 0 else { continue }
            let micRMS = (micEnergy / Float(need)).squareRoot()
            guard micRMS >= config.energyFloor else { continue }
            let corr = abs(dot / (refEnergy * micEnergy).squareRoot())
            if corr > best { best = corr }
        }
        return best
    }

    /// Convenience over raw PCM16LE mono data (little-endian).
    /// `referenceRate` is the reference's sample rate in Hz (Mac render tap:
    /// 48000); the microphone side is always 16 kHz. The reference is
    /// linearly resampled to 16 kHz first — timing precision loss (~1 sample)
    /// is negligible inside a +/-50 ms lag search. Unknown or absurd rates
    /// fail open: no data, no veto.
    public func vetoForLatest(
        referencePCM: Data, referenceRate: Double, microphonePCM: Data
    ) -> (veto: Bool, correlation: Float, detail: String) {
        // Same sanity band as TTS ingest: absurd rates would overflow the
        // resample sizing below (Double→Int traps), so refuse them here.
        guard referenceRate.isFinite, (8_000...96_000).contains(referenceRate) else {
            return (false, 0, "rate0")
        }
        let refRaw = tailInt16(referencePCM, maxSamples: resampledNeed(referenceRate: referenceRate))
        guard !refRaw.isEmpty else { return (false, 0, "refempty") }
        let ref = resampleLinear(refRaw, fromRate: referenceRate, toRate: 16_000)
        let mic = tailInt16(
            microphonePCM, maxSamples: config.windowSamples + 2 * config.maxLagSamples
        )
        guard ref.count >= config.windowSamples,
              mic.count >= config.windowSamples + 2 * config.maxLagSamples
        else {
            return (true, 0, "short")
        }
        let corr = peakCorrelation(reference: ref, microphone: mic)
        if corr == 0 {
            // Silence is ambiguous until the sides are split: a quiet mic
            // is a transient (veto), but a loud mic over a quiet reference
            // cannot be bleed — nothing audible to bleed from — so it must
            // be the owner cutting through a reply pause (confirm).
            let micLoud = int16RMS(mic) >= config.energyFloor
            return micLoud ? (false, 0, "refquiet") : (true, 0, "quiet")
        }
        return (corr >= config.vetoThreshold, corr, "ok")
    }

    private func resampledNeed(referenceRate: Double) -> Int {
        Int(Double(config.windowSamples) * referenceRate / 16_000) + 2
    }

    private func resampleLinear(
        _ samples: [Int16], fromRate: Double, toRate: Double
    ) -> [Int16] {
        guard fromRate > 0, toRate > 0, !samples.isEmpty else { return [] }
        if abs(fromRate - toRate) < 0.5 { return samples }
        let ratio = fromRate / toRate
        let count = Int(Double(samples.count) / ratio)
        guard count > 0 else { return [] }
        var out = [Int16]()
        out.reserveCapacity(count)
        for i in 0..<count {
            let pos = Double(i) * ratio
            let lo = Int(pos)
            let hi = min(lo + 1, samples.count - 1)
            let frac = Float(pos - Double(lo))
            let mixed = Float(samples[lo]) * (1 - frac) + Float(samples[hi]) * frac
            out.append(Int16(max(-32_768, min(32_767, Int(mixed)))))
        }
        return out
    }

    private func strideSlice(
        _ samples: [Int16], count: Int, decim: Int
    ) -> [Float] {
        let start = max(0, samples.count - count * decim)
        var out: [Float] = []
        out.reserveCapacity(count)
        var i = start
        while out.count < count, i < samples.count {
            out.append(Float(samples[i]))
            i += decim
        }
        return out
    }

    private func tailInt16(_ data: Data, maxSamples: Int) -> [Int16] {
        // Index math, not integer offsets: callers hand slice-backed Data
        // (non-zero startIndex), and integer subscripting traps on those
        // (EV crash 2026-10-08, tailInt16:149).
        let available = data.count / MemoryLayout<Int16>.size
        let take = min(available, maxSamples)
        guard take > 0 else { return [] }
        let byteCount = take * MemoryLayout<Int16>.size
        let startIndex = data.index(data.endIndex, offsetBy: -byteCount)
        return data[startIndex...].withUnsafeBytes { raw in
            Array(raw.bindMemory(to: Int16.self).map { Int16(littleEndian: $0) })
        }
    }

    private func rms(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else { return 0 }
        var energy: Float = 0
        for v in samples { energy += v * v }
        return (energy / Float(samples.count)).squareRoot()
    }

    private func int16RMS(_ samples: [Int16]) -> Float {
        guard !samples.isEmpty else { return 0 }
        var energy: Float = 0
        for v in samples { energy += Float(v) * Float(v) }
        return (energy / Float(samples.count)).squareRoot()
    }
}
