import Foundation
import PresenceGesture

/// Exhaustive state-machine checks for the Option-key gesture engine.
/// Every check drives a fresh engine with explicit virtual-time inputs.
enum GestureEngineChecks {
    typealias G = PresenceGesture
    typealias E = GestureEngine

    static func enabled(_ mutate: (inout G.Settings) -> Void = { _ in }) -> E {
        var s = G.Settings()
        s.enabled = true
        mutate(&s)
        return E(settings: s)
    }

    /// Feed inputs, collect every output in order.
    static func run(_ engine: inout E, _ inputs: [G.Input]) -> [G.Output] {
        var out: [G.Output] = []
        for i in inputs { out.append(contentsOf: engine.handle(i)) }
        return out
    }

    static func checkRun(
        _ name: String, _ check: (String, Bool, String) -> Void,
        _ condition: @autoclosure () -> Bool, _ detail: String = ""
    ) {
        check(name, condition(), detail)
    }

    static func run(_ check: (String, Bool, String) -> Void) {
        // MARK: Hold
        do {
            var e = enabled()
            let o1 = run(&e, [.optionDown(side: .left, at: 0, exclusive: true)])
            checkRun("hold-down-silent", check, o1.isEmpty)
            let o2 = e.handle(.tick(at: 219))
            checkRun("hold-before-threshold-silent", check, o2.isEmpty && !e.isListening)
            let o3 = e.handle(.tick(at: 220))
            checkRun("hold-fires-at-threshold", check, o3 == [.startListening(.hold)] && e.isListening)
            let o4 = e.handle(.optionUp(side: .left, at: 500))
            checkRun("hold-release-stops", check, o4 == [.stopListening(.released)] && !e.isListening)
            checkRun(
                "hold-stats", check,
                e.stats.activations == 1 && e.stats.stops == 1,
                "\(e.stats)"
            )
        }

        // A press released before the threshold is a tap, not a hold.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
            ])
            checkRun("tap-silent", check, o.isEmpty && e.stats.taps == 1 && e.stats.activations == 0)
            checkRun("tap-pending-state", check, e.stateDescription == "tap-pending")
            _ = e.handle(.tick(at: 460)) // 100 + 360 > 350 window
            checkRun("tap-expires-silently", check, e.stateDescription == "idle")
            _ = e.handle(.optionDown(side: .left, at: 1000, exclusive: true))
            checkRun("tap-expiry-allows-fresh-press", check, e.stateDescription == "down")
        }

        // MARK: Double-tap latch
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .right, at: 0, exclusive: true),
                .optionUp(side: .right, at: 100),
                .optionDown(side: .right, at: 200, exclusive: true),
                .optionUp(side: .right, at: 300),
            ])
            checkRun(
                "double-tap-latches", check,
                o == [.startListening(.doubleTap)] && e.isLatched && e.isListening,
                "\(o)"
            )
        }

        // Window edge: release@100, second press at exactly +350 still counts.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 450, exclusive: true),
                .optionUp(side: .left, at: 500),
            ])
            checkRun("double-window-edge-latches", check, o == [.startListening(.doubleTap)], "\(o)")
        }

        // 1 ms past the window: the second press is a fresh first press.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 451, exclusive: true),
                .optionUp(side: .left, at: 551),
            ])
            checkRun(
                "double-window-miss-no-latch", check,
                o.isEmpty && e.stats.taps == 2 && e.stats.activations == 0,
                "\(o) \(e.stats)"
            )
        }

        // Triple tap: the third tap unlatches.
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            let o = run(&e, [
                .optionDown(side: .left, at: 1000, exclusive: true),
                .optionUp(side: .left, at: 1100),
            ])
            checkRun(
                "triple-tap-unlatches", check,
                o == [.stopListening(.tapped)] && !e.isListening,
                "\(o)"
            )
            // The unlatch release is not a tap: a following press starts fresh.
            let o2 = run(&e, [
                .optionDown(side: .left, at: 1200, exclusive: true),
                .optionUp(side: .left, at: 1300),
            ])
            checkRun(
                "post-unlatch-press-is-fresh-tap", check,
                o2.isEmpty && e.stats.taps == 2 && e.stats.activations == 1,
                "\(o2) \(e.stats)"
            )
        }

        // MARK: Tap + key, chord guard
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .otherKey(at: 150),
            ])
            let o = run(&e, [
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            checkRun(
                "tap-then-key-cancels-double", check,
                o.isEmpty && e.stats.activations == 0 && e.stats.taps == 2,
                "\(o) \(e.stats)"
            )
        }

        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .otherKey(at: 50),
                .optionUp(side: .left, at: 100),
                .modifiersReleased(at: 120),
            ])
            checkRun(
                "option-plus-key-blocked", check,
                o.isEmpty && e.stats.chordBlocks == 1 && e.stateDescription == "idle",
                "\(o) \(e.stats)"
            )
        }

        // Blocked until ALL modifiers release: presses in between are dead.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .otherKey(at: 50),
                .optionDown(side: .left, at: 80, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 110, exclusive: true),
                .optionUp(side: .left, at: 115),
                .modifiersReleased(at: 130),
            ])
            checkRun(
                "blocked-until-release", check,
                o.isEmpty && e.stats.activations == 0 && e.stateDescription == "idle",
                "\(o)"
            )
            _ = e.handle(.optionDown(side: .left, at: 200, exclusive: true))
            checkRun("release-clears-block", check, e.stateDescription == "down")
        }

        for (name, chord) in [
            ("click", G.Input.mouseDown(at: 40)),
            ("scroll", G.Input.scroll(at: 40)),
            ("extra-modifier", G.Input.extraModifier(at: 40)),
        ] {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                chord,
                .optionUp(side: .left, at: 100),
                .modifiersReleased(at: 120),
            ])
            checkRun(
                "option-plus-\(name)-blocked", check,
                o.isEmpty && e.stats.activations == 0 && e.stats.chordBlocks == 1,
                "\(o) \(e.stats)"
            )
        }

        // Cmd+Option style: another modifier was already held at press time.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: false),
                .otherKey(at: 30),
                .optionUp(side: .left, at: 60),
                .modifiersReleased(at: 80),
            ])
            checkRun(
                "nonexclusive-down-blocked", check,
                o.isEmpty && e.stats.chordBlocks == 1,
                "\(o) \(e.stats)"
            )
        }

        // Both Options at once is not a gesture.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionDown(side: .right, at: 50, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionUp(side: .right, at: 110),
                .modifiersReleased(at: 120),
            ])
            checkRun("both-options-blocked", check, o.isEmpty && e.stats.activations == 0, "\(o)")
        }

        // Duplicate same-side down (lost up + repeat): stay down, no output.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionDown(side: .left, at: 50, exclusive: true),
                .tick(at: 300),
            ])
            checkRun(
                "duplicate-same-side-down-stays-down", check,
                o == [.startListening(.hold)],
                "\(o)"
            )
        }

        // A key during an active hold proves the hold was typing: stop + block.
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .tick(at: 220),
            ])
            let o = run(&e, [
                .otherKey(at: 300),
                .optionUp(side: .left, at: 400),
                .modifiersReleased(at: 420),
            ])
            checkRun(
                "hold-then-key-cancels", check,
                o == [.stopListening(.cancelled)] && e.stateDescription == "idle",
                "\(o)"
            )
        }

        // Tap-then-hold is a hold: the first tap is consumed, no latch.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .tick(at: 420),
            ])
            checkRun("tap-then-hold-is-hold", check, o == [.startListening(.hold)], "\(o)")
            let o2 = e.handle(.optionUp(side: .left, at: 500))
            checkRun("tap-then-hold-release", check, o2 == [.stopListening(.released)], "\(o2)")
            checkRun("tap-then-hold-no-latch", check, !e.isLatched && e.stats.activations == 1)
        }

        // Bounce on the second press reverts to pending; a real tap latches.
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 210),
            ])
            checkRun(
                "second-press-bounce-reverts", check,
                o.isEmpty && e.stateDescription == "tap-pending" && e.stats.bounces == 1,
                "\(o) \(e.stateDescription)"
            )
            let o2 = run(&e, [
                .optionDown(side: .left, at: 300, exclusive: true),
                .optionUp(side: .left, at: 350),
            ])
            checkRun("bounce-then-real-tap-latches", check, o2 == [.startListening(.doubleTap)], "\(o2)")
        }

        // MARK: Jitter / bounce
        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 10),
            ])
            checkRun(
                "bounce-ignored", check,
                o.isEmpty && e.stats.bounces == 1 && e.stats.taps == 0 && e.stateDescription == "idle",
                "\(o) \(e.stats)"
            )
        }

        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 10),
                .optionDown(side: .left, at: 50, exclusive: true),
                .optionUp(side: .left, at: 60),
            ])
            checkRun(
                "double-bounce-no-latch", check,
                o.isEmpty && e.stats.activations == 0 && e.stats.taps == 0,
                "\(o) \(e.stats)"
            )
        }

        // MARK: Lost key-up
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .tick(at: 300),
            ])
            let o = e.handle(.resync(optionPhysicalDown: false, at: 1000))
            checkRun(
                "lost-up-during-hold-resyncs", check,
                o == [.stopListening(.released)] && e.stats.lostKeyUpRescues == 1,
                "\(o) \(e.stats)"
            )
        }

        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .modifiersReleased(at: 100),
            ])
            checkRun(
                "lost-up-short-release-is-tap", check,
                o.isEmpty && e.stats.taps == 1 && e.stats.lostKeyUpRescues == 1,
                "\(o) \(e.stats)"
            )
        }

        // A press we never saw the start of: blocked, never activated.
        do {
            var e = enabled()
            let o = e.handle(.resync(optionPhysicalDown: true, at: 0))
            checkRun(
                "missed-down-adopted-blocked", check,
                o.isEmpty && e.stateDescription == "chord-blocked" && e.stats.resyncs == 1,
                "\(o)"
            )
            _ = e.handle(.resync(optionPhysicalDown: false, at: 100))
            checkRun("missed-down-clear", check, e.stateDescription == "idle")
        }

        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .resync(optionPhysicalDown: true, at: 50),
                .optionUp(side: .left, at: 100),
            ])
            checkRun("resync-agree-noop", check, o.isEmpty && e.stats.taps == 1, "\(o)")
        }

        // MARK: Tap latch disabled
        do {
            var e = enabled { $0.tapLatchEnabled = false }
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            checkRun(
                "latch-disabled-double-silent", check,
                o.isEmpty && e.stats.activations == 0,
                "\(o)"
            )
            let o2 = run(&e, [
                .optionDown(side: .left, at: 1000, exclusive: true),
                .tick(at: 1300),
            ])
            checkRun("latch-disabled-hold-works", check, o2 == [.startListening(.hold)], "\(o2)")
        }

        // MARK: Escape
        do {
            var e = enabled()
            let o = e.handle(.escape(at: 0))
            checkRun("esc-idle-cancels-turn-only", check, o == [.cancelTurn], "\(o)")
        }

        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .tick(at: 220),
            ])
            let o = run(&e, [
                .escape(at: 300),
                .optionUp(side: .left, at: 400),
                .modifiersReleased(at: 420),
            ])
            checkRun(
                "esc-while-hold", check,
                o == [.stopListening(.cancelled), .cancelTurn] && e.stateDescription == "idle",
                "\(o)"
            )
        }

        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            let o = e.handle(.escape(at: 2000))
            checkRun(
                "esc-while-latched", check,
                o == [.stopListening(.cancelled), .cancelTurn] && e.stateDescription == "idle",
                "\(o)"
            )
        }

        do {
            var e = enabled()
            let o = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .escape(at: 50),
                .optionUp(side: .left, at: 100),
                .modifiersReleased(at: 120),
            ])
            checkRun(
                "esc-during-press-blocks", check,
                o == [.cancelTurn] && e.stats.chordBlocks == 1 && e.stateDescription == "idle",
                "\(o) \(e.stats)"
            )
        }

        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .escape(at: 150),
            ])
            let o = run(&e, [
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            checkRun(
                "esc-clears-pending", check,
                o.isEmpty && e.stats.activations == 0 && e.stats.taps == 2,
                "\(o) \(e.stats)"
            )
        }

        // MARK: Latch timing (virtual clock jumps)
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            let w = e.handle(.tick(at: 300 + 540_000))
            checkRun(
                "latch-warns-at-nine-min", check,
                w == [.latchWarning(remainingMs: 60_000)],
                "\(w)"
            )
            let w2 = e.handle(.tick(at: 300 + 550_000))
            checkRun("latch-warning-once", check, w2.isEmpty, "\(w2)")
            let x = e.handle(.tick(at: 300 + 600_000))
            checkRun(
                "latch-expires-at-ten-min", check,
                x == [.stopListening(.latchExpired)] && !e.isListening,
                "\(x)"
            )
        }

        // The unlatch press may be any length: no hold may fire from it.
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            let o = run(&e, [
                .optionDown(side: .left, at: 1000, exclusive: true),
                .tick(at: 2000),
                .optionUp(side: .left, at: 2500),
            ])
            checkRun(
                "unlatch-press-any-length", check,
                o == [.stopListening(.tapped)] && e.stats.activations == 1,
                "\(o) \(e.stats)"
            )
        }

        // Typing / clicking / scrolling while latched (Option up) is ignored.
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            let o = run(&e, [
                .otherKey(at: 1000),
                .mouseDown(at: 1100),
                .scroll(at: 1200),
                .extraModifier(at: 1300),
            ])
            checkRun("latch-survives-typing", check, o.isEmpty && e.isLatched, "\(o)")
        }

        // Unlatch press turning into typing: unlatch cancelled, latch resumes.
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 300),
            ])
            let o = run(&e, [
                .optionDown(side: .left, at: 1000, exclusive: true),
                .otherKey(at: 1100),
                .optionUp(side: .left, at: 1200),
                .modifiersReleased(at: 1300),
            ])
            checkRun(
                "unlatch-chord-resumes-latch", check,
                o.isEmpty && e.isListening && e.stats.stops == 0,
                "\(o) \(e.stats)"
            )
            let o2 = run(&e, [
                .optionDown(side: .left, at: 1400, exclusive: true),
                .optionUp(side: .left, at: 1500),
            ])
            checkRun("resumed-latch-unlatches", check, o2 == [.stopListening(.tapped)], "\(o2)")
        }

        // MARK: Master switch (default off)
        do {
            let e = E() // defaults: enabled == false
            checkRun("default-off", check, e.settings.enabled == false)
            var e2 = e
            let o = run(&e2, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .tick(at: 500),
                .optionUp(side: .left, at: 600),
                .escape(at: 700),
            ])
            checkRun("disabled-ignores-all", check, o.isEmpty && e2.stats.activations == 0, "\(o)")
        }

        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .tick(at: 220),
            ])
            let o = e.handle(.setEnabled(false, at: 300))
            checkRun(
                "disable-while-listening-stops", check,
                o == [.stopListening(.disabled)] && !e.isListening,
                "\(o)"
            )
            let o2 = run(&e, [
                .optionDown(side: .left, at: 400, exclusive: true),
                .tick(at: 700),
            ])
            checkRun("disabled-stays-dead", check, o2.isEmpty, "\(o2)")
            _ = e.handle(.setEnabled(true, at: 800))
            let o3 = run(&e, [
                .optionDown(side: .left, at: 900, exclusive: true),
                .tick(at: 1200),
            ])
            checkRun("reenable-resets", check, o3 == [.startListening(.hold)], "\(o3)")
        }

        // MARK: Suspend (secure input / tap disabled)
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .tick(at: 220),
            ])
            let o = e.handle(.suspend(at: 300))
            checkRun(
                "suspend-stops-listening", check,
                o == [.stopListening(.suspended)] && e.stateDescription == "suspended",
                "\(o)"
            )
            let o2 = run(&e, [
                .optionDown(side: .left, at: 400, exclusive: true),
                .tick(at: 700),
            ])
            checkRun("suspended-ignores-input", check, o2.isEmpty, "\(o2)")
            _ = e.handle(.resume(at: 800))
            checkRun("resume-idles", check, e.stateDescription == "idle")
            let o3 = run(&e, [
                .optionDown(side: .left, at: 900, exclusive: true),
                .tick(at: 1200),
            ])
            checkRun("post-resume-works", check, o3 == [.startListening(.hold)], "\(o3)")
        }

        do {
            var e = enabled()
            let o = e.handle(.suspend(at: 0))
            checkRun("suspend-idle-silent", check, o.isEmpty && e.stats.suspends == 1, "\(o)")
        }

        // MARK: Option side scope
        do {
            var e = enabled { $0.optionScope = .left }
            let o = run(&e, [
                .optionDown(side: .right, at: 0, exclusive: true),
                .tick(at: 500),
                .optionUp(side: .right, at: 600),
            ])
            checkRun("scope-left-ignores-right", check, o.isEmpty && e.stats.activations == 0, "\(o)")
            let o2 = run(&e, [
                .optionDown(side: .left, at: 1000, exclusive: true),
                .tick(at: 1300),
            ])
            checkRun("scope-left-accepts-left", check, o2 == [.startListening(.hold)], "\(o2)")
        }

        do {
            var e = enabled { $0.optionScope = .right }
            _ = run(&e, [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 100),
            ])
            checkRun("scope-right-ignores-left-tap", check, e.stats.taps == 0)
            let o = run(&e, [
                .optionDown(side: .right, at: 1000, exclusive: true),
                .optionUp(side: .right, at: 1100),
                .optionDown(side: .right, at: 1200, exclusive: true),
                .optionUp(side: .right, at: 1300),
            ])
            checkRun("scope-right-double-latches", check, o == [.startListening(.doubleTap)], "\(o)")
            // Wrong-side press while latched: ignored, latch survives.
            _ = e.handle(.optionDown(side: .left, at: 2000, exclusive: true))
            checkRun("unlatch-wrong-side-ignored", check, e.isLatched)
            _ = e.handle(.optionUp(side: .left, at: 2100))
            let o2 = run(&e, [
                .optionDown(side: .right, at: 2200, exclusive: true),
                .optionUp(side: .right, at: 2300),
            ])
            checkRun("unlatch-right-side", check, o2 == [.stopListening(.tapped)], "\(o2)")
        }

        // MARK: Time travel is clamped, never fatal
        do {
            var e = enabled()
            _ = run(&e, [
                .optionDown(side: .left, at: 1000, exclusive: true),
                .tick(at: 500), // backwards: clamped to 1000
                .tick(at: 1220), // 1000 + 220: hold fires
            ])
            checkRun("time-travel-clamped", check, e.isListening)
        }
    }
}
