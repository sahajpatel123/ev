import Foundation
import PresenceGesture

/// Deterministic heavy-Option typing replay: proves the chord guard produces
/// ZERO accidental activations across >= 10,000 synthetic events, and a
/// gesture-positive control replay proving the machine is not simply dead.
///
/// Seeded xorshift64: identical sequence on every run, no GameplayKit.
enum TypingReplay {
    struct RNG {
        var state: UInt64
        mutating func next() -> UInt64 {
            var x = state
            x ^= x >> 12
            x ^= x << 25
            x ^= x >> 27
            state = x
            return x &* 0x2545F4914F6CDD1D
        }
        mutating func below(_ n: Int) -> Int { Int(next() % UInt64(max(n, 1))) }
        mutating func range(_ lo: Int, _ hi: Int) -> Int { lo + below(hi - lo + 1) }
    }

    /// One atom of realistic typing. Gaps are inter-input ms.
    static func generate(count: Int, seed: UInt64) -> [PresenceGesture.Input] {
        typealias I = PresenceGesture.Input
        var rng = RNG(state: seed == 0 ? 0x9E3779B97F4A7C15 : seed)
        var out: [I] = []
        out.reserveCapacity(count)
        var t = 0
        func gap(_ lo: Int, _ hi: Int) { t += rng.range(lo, hi) }
        func side() -> PresenceGesture.OptionSide { rng.below(2) == 0 ? .left : .right }

        while out.count < count {
            switch rng.below(100) {
            case 0..<30:
                // Plain typing burst: 1-6 keys.
                for _ in 0..<rng.range(1, 6) {
                    gap(15, 220); out.append(.otherKey(at: t))
                }
            case 30..<52:
                // Option+letter (special characters, symbols): the classic
                // false-trigger shape. Chorded within 150 ms.
                let s = side()
                gap(10, 400); out.append(.optionDown(side: s, at: t, exclusive: true))
                gap(20, 150); out.append(.otherKey(at: t))
                gap(10, 120); out.append(.optionUp(side: s, at: t))
                if rng.below(2) == 0 { gap(0, 30); out.append(.modifiersReleased(at: t)) }
            case 52..<62:
                // Option held across several letters (dead-key accent runs).
                let s = side()
                gap(10, 400); out.append(.optionDown(side: s, at: t, exclusive: true))
                for _ in 0..<rng.range(2, 4) {
                    gap(30, 200); out.append(.otherKey(at: t))
                }
                gap(10, 150); out.append(.optionUp(side: s, at: t))
                gap(0, 30); out.append(.modifiersReleased(at: t))
            case 62..<72:
                // Cmd+Option / Ctrl+Option shortcuts (exclusive=false).
                let s = side()
                gap(10, 500); out.append(.optionDown(side: s, at: t, exclusive: false))
                gap(20, 180); out.append(.otherKey(at: t))
                gap(10, 150); out.append(.optionUp(side: s, at: t))
                gap(0, 60); out.append(.modifiersReleased(at: t))
            case 72..<80:
                // Option-click / Option-scroll.
                let s = side()
                gap(10, 500); out.append(.optionDown(side: s, at: t, exclusive: true))
                gap(20, 180)
                out.append(rng.below(2) == 0 ? .mouseDown(at: t) : .scroll(at: t))
                gap(10, 150); out.append(.optionUp(side: s, at: t))
                gap(0, 60); out.append(.modifiersReleased(at: t))
            case 80..<88:
                // Lone Option tap followed by typing: the pending double is
                // always cancelled by a key before any second press, so no
                // latch is possible — exactly like real typing rhythm.
                let s = side()
                gap(60, 600); out.append(.optionDown(side: s, at: t, exclusive: true))
                gap(40, 150); out.append(.optionUp(side: s, at: t))
                gap(20, 100); out.append(.otherKey(at: t))
                if rng.below(2) == 0 { gap(0, 30); out.append(.modifiersReleased(at: t)) }
            case 88..<94:
                // Clicks and scrolls without Option.
                gap(20, 600)
                out.append(rng.below(2) == 0 ? .mouseDown(at: t) : .scroll(at: t))
            case 94..<97:
                // Bare extra-modifier taps (Shift/Cmd alone).
                gap(20, 400); out.append(.extraModifier(at: t))
            default:
                // Long pause: breaks every pending window.
                gap(400, 2500); out.append(.tick(at: t))
            }
        }
        return out
    }

    /// Scripted control: 5 holds + 3 latch/unlatch cycles inside typing noise.
    /// The machine must report exactly 8 activations / 8 stops.
    static func controlSequence() -> [PresenceGesture.Input] {
        typealias I = PresenceGesture.Input
        var seq: [I] = []
        var t = 0
        func hold(_ ms: Int) {
            t += 500; seq.append(.optionDown(side: .left, at: t, exclusive: true))
            t += 220; seq.append(.tick(at: t))
            t += ms; seq.append(.optionUp(side: .left, at: t))
            t += 50; seq.append(.otherKey(at: t)) // typing between gestures
        }
        func latchCycle() {
            t += 500; seq.append(.optionDown(side: .left, at: t, exclusive: true))
            t += 90; seq.append(.optionUp(side: .left, at: t))
            t += 150; seq.append(.optionDown(side: .left, at: t, exclusive: true))
            t += 90; seq.append(.optionUp(side: .left, at: t))
            t += 400; seq.append(.otherKey(at: t)) // typing while latched: ignored
            t += 300; seq.append(.optionDown(side: .left, at: t, exclusive: true))
            t += 90; seq.append(.optionUp(side: .left, at: t))
        }
        // Interleave with chorded Option typing that must never fire.
        hold(600)
        t += 200; seq.append(.optionDown(side: .right, at: t, exclusive: true))
        t += 60; seq.append(.otherKey(at: t))
        t += 60; seq.append(.optionUp(side: .right, at: t))
        t += 20; seq.append(.modifiersReleased(at: t))
        hold(300)
        latchCycle()
        hold(900)
        latchCycle()
        hold(250)
        latchCycle()
        hold(1200)
        return seq
    }

    static func run(_ check: (String, Bool, String) -> Void) {
        // The zero-false-activation proof: >= 10,000 events, heavy Option use.
        let events = generate(count: 12_000, seed: 0xE71CE)
        var settings = PresenceGesture.Settings()
        settings.enabled = true
        var engine = GestureEngine(settings: settings)
        var sawOutput = 0
        for input in events {
            sawOutput += engine.handle(input).count
        }
        let stats = engine.stats
        check(
            "replay-event-count",
            events.count >= 10_000,
            "got \(events.count)"
        )
        check(
            "replay-zero-activations",
            stats.activations == 0,
            "activations=\(stats.activations)"
        )
        check(
            "replay-zero-stops",
            stats.stops == 0,
            "stops=\(stats.stops)"
        )
        check("replay-zero-outputs", sawOutput == 0, "outputs=\(sawOutput)")
        check(
            "replay-guard-engaged",
            stats.chordBlocks > 100,
            "chordBlocks=\(stats.chordBlocks)"
        )
        check("replay-taps-seen", stats.taps > 0, "taps=\(stats.taps)")
        print(
            "INFO replay events=\(events.count) chordBlocks=\(stats.chordBlocks)" +
                " taps=\(stats.taps) bounces=\(stats.bounces)" +
                " activations=\(stats.activations)"
        )

        // Second seed: the proof must not depend on one sequence.
        let events2 = generate(count: 10_000, seed: 0x0DD710)
        var engine2 = GestureEngine(settings: settings)
        var outs2 = 0
        for input in events2 { outs2 += engine2.handle(input).count }
        check(
            "replay-zero-activations-seed2",
            engine2.stats.activations == 0 && outs2 == 0,
            "activations=\(engine2.stats.activations) outputs=\(outs2)"
        )

        // Control: the same engine DOES fire on real gestures in noise.
        let control = controlSequence()
        var engine3 = GestureEngine(settings: settings)
        var reasons: [PresenceGesture.ListenReason] = []
        var stopReasons: [PresenceGesture.StopReason] = []
        for input in control {
            for o in engine3.handle(input) {
                switch o {
                case .startListening(let r): reasons.append(r)
                case .stopListening(let r): stopReasons.append(r)
                default: break
                }
            }
        }
        check(
            "control-activations",
            engine3.stats.activations == 8,
            "got \(engine3.stats.activations)"
        )
        check(
            "control-stops",
            engine3.stats.stops == 8,
            "got \(engine3.stats.stops)"
        )
        check(
            "control-reasons",
            reasons.filter({ $0 == .hold }).count == 5
                && reasons.filter({ $0 == .doubleTap }).count == 3,
            "\(reasons)"
        )
        check(
            "control-stop-reasons",
            stopReasons.filter({ $0 == .released }).count == 5
                && stopReasons.filter({ $0 == .tapped }).count == 3,
            "\(stopReasons)"
        )
        check(
            "control-quiescent",
            !engine3.isListening && engine3.stateDescription == "idle",
            engine3.stateDescription
        )
    }
}
