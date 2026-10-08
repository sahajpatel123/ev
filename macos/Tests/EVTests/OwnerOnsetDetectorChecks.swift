import EVRuntime
import Foundation

/// Interrupt V2 onset-detector checks. Invoked from EVMicTalkTests.main.
enum OwnerOnsetDetectorChecks {
    static func run(_ check: (String, Bool, String) -> Void) {
        var detector = OwnerOnsetDetector()
        let t0 = Date()

        // Silence during playback: no onset, no streak.
        check(
            "onset-silence-no-confirm",
            detector.poll(input: 0.02, output: 0.5, echoGate: true, now: t0) == nil,
            "quiet mic must not confirm"
        )

        // Playback bleed below the differential margin: no confirm.
        var bleed = OwnerOnsetDetector()
        var bleedConfirmed = false
        for i in 0..<6 {
            if bleed.poll(input: 0.35, output: 0.5, echoGate: true, now: t0.addingTimeInterval(Double(i) * 0.1)) != nil {
                bleedConfirmed = true
            }
        }
        check(
            "onset-bleed-no-confirm",
            !bleedConfirmed,
            "mic under output*1.5+0.10 must not confirm (0.35 < 0.85)"
        )

        // Clear onset above margin for 3 polls: confirms once.
        var onset = OwnerOnsetDetector()
        var confirms = 0
        var confidence = 0.0
        var speechMs = 0
        for i in 0..<5 {
            if let done = onset.poll(input: 0.9, output: 0.4, echoGate: true, now: t0.addingTimeInterval(Double(i) * 0.1)) {
                confirms += 1
                confidence = done.confidence
                speechMs = done.speechMs
            }
        }
        check("onset-clear-confirms-once", confirms == 1, "expected 1, got \(confirms)")
        check("onset-confidence-clears-server-bar", confidence >= 0.6, "got \(confidence)")
        check("onset-speech-ms-clears-server-floor", speechMs >= 160, "got \(speechMs)")

        // Broken streak resets: 2 hits, miss, 2 hits must not confirm.
        var broken = OwnerOnsetDetector()
        let levels: [Float] = [0.9, 0.9, 0.05, 0.9, 0.9]
        var brokenConfirmed = false
        for (i, level) in levels.enumerated() {
            if broken.poll(input: level, output: 0.4, echoGate: true, now: t0.addingTimeInterval(Double(i) * 0.1)) != nil {
                brokenConfirmed = true
            }
        }
        check("onset-broken-streak-no-confirm", !brokenConfirmed, "a miss must reset persistence")

        // Cooldown: a second onset within 2 s does not confirm.
        var cool = OwnerOnsetDetector()
        for i in 0..<3 {
            _ = cool.poll(input: 0.9, output: 0.4, echoGate: true, now: t0.addingTimeInterval(Double(i) * 0.1))
        }
        var refired = false
        for i in 3..<7 {
            if cool.poll(input: 0.9, output: 0.4, echoGate: true, now: t0.addingTimeInterval(Double(i) * 0.1)) != nil {
                refired = true
            }
        }
        check("onset-cooldown-no-refire", !refired, "2 s refractory must hold")

        // Gate closed (Eve silent): never confirms, streak cleared.
        var gated = OwnerOnsetDetector()
        check(
            "onset-gate-closed-no-confirm",
            gated.poll(input: 0.95, output: 0.0, echoGate: false, now: t0) == nil,
            "no gate means nothing to cut"
        )

        // Preroll ring: bounded, flushable, honest millisecond math.
        let ring = OnsetPrerollRing(capBytes: 3200)
        ring.append(Data(repeating: 0, count: 1600))
        check("onset-ring-buffers", ring.bufferedMs == 50, "1600 bytes must read 50 ms")
        ring.append(Data(repeating: 0, count: 3200))
        check("onset-ring-capped", ring.bufferedMs == 100, "cap must hold at 3200 bytes")
        let flushed = ring.flush()
        check("onset-ring-flush-empties", flushed.count == 3200 && ring.bufferedMs == 0, "flush must drain")
    }
}
