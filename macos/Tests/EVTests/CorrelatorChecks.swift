import EVRuntime
import Foundation

/// Reference-correlation veto checks. Deterministic sine fixtures stand in
/// for speech: the math (alignment-insensitive normalized cross-correlation)
/// is identical, and real-speech separation is proven by the prototype
/// (`/tmp/corr_proto.py` during development: bleed 0.90+, owner 0.05).
enum CorrelatorChecks {
    static let rate = 16_000

    static func sine(freq: Double, seconds: Double, amp: Int16) -> [Int16] {
        let n = Int(Double(rate) * seconds)
        return (0..<n).map { i in
            Int16(Double(amp) * sin(2 * .pi * freq * Double(i) / Double(rate)))
        }
    }

    static func run(_ check: (String, Bool, String) -> Void) {
        let corr = ReferenceCorrelator()
        let window = 3840 // 240 ms at 16 kHz
        let span = window + 1600
        let ref = sine(freq: 440, seconds: 0.30, amp: 12000)

        // Pure bleed: attenuated + 15 ms delayed reference in noise.
        // Must veto (correlation near 1, gain-independent).
        var bleed = [Int16](repeating: 0, count: span)
        let delay = 240
        for i in 0..<window {
            let mixed = Int32(ref[i]) * 3 / 10 + Int32(i % 7) * 40 - 120
            bleed[800 + delay + i] = Int16(max(-32_768, min(32_767, mixed)))
        }
        let bleedCorr = corr.peakCorrelation(
            reference: Array(ref[0..<window]),
            microphone: bleed
        )
        check("corr-bleed-vetoes", bleedCorr >= 0.65, "got \(bleedCorr)")

        // Gain independence: same bleed at 1/8 amplitude must still veto.
        var quiet = [Int16](repeating: 0, count: span)
        for i in 0..<window {
            quiet[800 + delay + i] = ref[i] / 8
        }
        let quietCorr = corr.peakCorrelation(
            reference: Array(ref[0..<window]),
            microphone: quiet
        )
        check("corr-quiet-bleed-vetoes", quietCorr >= 0.65, "got \(quietCorr)")

        // Owner alone: unrelated voice (different pitch) must NOT veto.
        let owner = sine(freq: 617, seconds: 0.34, amp: 12000)
        let ownerCorr = corr.peakCorrelation(
            reference: Array(ref[0..<window]),
            microphone: Array(owner[0..<span])
        )
        check("corr-owner-passes", ownerCorr < 0.65, "got \(ownerCorr)")

        // Double-talk: owner at full with bleed underneath must NOT veto.
        var mixed = Array(owner[0..<span])
        for i in 0..<window {
            let sum = Int32(mixed[800 + delay + i]) + Int32(ref[i]) / 2
            mixed[800 + delay + i] = Int16(max(-32_768, min(32_767, sum)))
        }
        let mixedCorr = corr.peakCorrelation(
            reference: Array(ref[0..<window]),
            microphone: mixed
        )
        check("corr-doubletalk-passes", mixedCorr < 0.65, "got \(mixedCorr)")

        // Silence on either side: no information, never veto.
        let silent = [Int16](repeating: 0, count: span)
        let refWindow = Array(ref[0..<window])
        check(
            "corr-silent-mic-passes",
            corr.peakCorrelation(reference: refWindow, microphone: silent) == 0,
            "quiet mic must yield exactly 0"
        )
        check(
            "corr-silent-ref-passes",
            corr.peakCorrelation(
                reference: [Int16](repeating: 0, count: window), microphone: mixed
            ) == 0,
            "quiet ref must yield exactly 0"
        )

        // Short spans: insufficient data must fail open (no veto), never trap.
        check(
            "corr-short-span-passes",
            corr.peakCorrelation(
                reference: Array(ref[0..<100]), microphone: Array(mixed[0..<200])
            ) == 0,
            "short spans must yield exactly 0"
        )

        // Slice-backed Data (non-zero startIndex, as handed by the preroll
        // ring's suffix and TTS snapshots) must not trap: EV crashed here
        // on 2026-10-08 (tailInt16:149) the first time a live onset ran the
        // veto. Slice the window out of a larger buffer so startIndex > 0.
        func pcm16(_ samples: [Int16]) -> Data {
            var data = Data(capacity: samples.count * 2)
            for sample in samples {
                var little = sample.littleEndian
                withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
            }
            return data
        }
        let bigRef = pcm16(ref) + pcm16(ref)
        let bigMic = pcm16(bleed) + pcm16(bleed)
        let slicedRef = bigRef.suffix(ref.count * 2)
        let slicedMic = bigMic.suffix(bleed.count * 2)
        check(
            "corr-sliced-input-precondition",
            slicedRef.startIndex > 0 && slicedMic.startIndex > 0,
            "fixtures must actually be non-zero-based slices"
        )
        let sliced = corr.vetoForLatest(
            referencePCM: slicedRef, referenceRate: 16_000,
            microphonePCM: slicedMic
        )
        check(
            "corr-sliced-bleed-vetoes",
            sliced.veto && sliced.correlation >= 0.65 && sliced.detail == "ok",
            "sliced bleed must veto, got \(sliced.correlation) \(sliced.detail)"
        )
        check(
            "corr-detail-failopen",
            corr.vetoForLatest(
                referencePCM: pcm16(ref), referenceRate: 0,
                microphonePCM: pcm16(bleed)
            ).detail == "rate0",
            "unknown rate must report rate0"
        )
        check(
            "corr-absurd-rate-failopen",
            corr.vetoForLatest(
                referencePCM: pcm16(ref), referenceRate: 1e12,
                microphonePCM: pcm16(bleed)
            ).detail == "rate0"
                && corr.vetoForLatest(
                    referencePCM: pcm16(ref), referenceRate: .infinity,
                    microphonePCM: pcm16(bleed)
                ).detail == "rate0",
            "absurd rates must report rate0, never trap"
        )

        // Undecidable windows veto (fail closed): the detector re-polls in
        // 100 ms, so a genuine cut-in only waits for buffers to fill, while
        // fail-open would self-cut Eve on early-reply bleed onsets.
        let shortVerdict = corr.vetoForLatest(
            referencePCM: pcm16(Array(ref[0..<100])), referenceRate: 16_000,
            microphonePCM: pcm16(Array(mixed[0..<200]))
        )
        check(
            "corr-short-vetoes",
            shortVerdict.veto && shortVerdict.detail == "short",
            "short spans must veto, got \(shortVerdict.detail)"
        )
        let quietVerdict = corr.vetoForLatest(
            referencePCM: pcm16(refWindow), referenceRate: 16_000,
            microphonePCM: pcm16(silent)
        )
        check(
            "corr-quiet-vetoes",
            quietVerdict.veto && quietVerdict.detail == "quiet",
            "quiet spans must veto, got \(quietVerdict.detail)"
        )
        // Loud mic over a quiet reference cannot be bleed (nothing audible
        // to bleed from): the owner cutting through a reply pause. Confirm.
        let refQuietVerdict = corr.vetoForLatest(
            referencePCM: pcm16([Int16](repeating: 0, count: window)),
            referenceRate: 16_000,
            microphonePCM: pcm16(Array(owner[0..<span]))
        )
        check(
            "corr-refquiet-confirms",
            !refQuietVerdict.veto && refQuietVerdict.detail == "refquiet",
            "loud-mic-quiet-ref must confirm, got \(refQuietVerdict.detail)"
        )
    }
}
