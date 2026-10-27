import Foundation
import PresenceGesture

/// 60-second gesture calibration (owner-run, interactive).
///
/// 1. Holds (≤15 s): hold+release Option 5+ times — records durations.
/// 2. Doubles (≤15 s): double-tap Option 3+ times — records taps/gaps.
/// 3. Typing (30 s): type normally with heavy Option use — must produce
///    ZERO activations or the whole check FAILs.
/// Report to `~/.ev/presence-calibration.json` + stdout. Never auto-tunes:
/// on PASS it prints the one `defaults write` that enables gestures.
enum CalibrateFlow {
    static let reportPath = "~/.ev/presence-calibration.json"

    static func run(json: Bool) -> Int32 {
        guard GestureTapSource.inputMonitoringGranted() else {
            print("FAIL: Input Monitoring is not granted to this tool.")
            print("Run `presence-check permissions` for the fix, then re-run.")
            return 2
        }
        let source = GestureTapSource()
        guard source.start() else {
            print("FAIL: could not install the listen-only tap.")
            return 2
        }
        defer { source.stop() }
        var settings = PresenceGesture.Settings()
        settings.enabled = true
        var engine = GestureEngine(settings: settings)

        print("=== Gesture calibration (≤60 s of measurement) ===")
        print("This tap is listen-only: nothing you press is consumed.")
        print("")

        // Phase 1: holds.
        PresenceCheck.waitEnter("Phase 1/3 — HOLD: press Enter, then hold+release Option 5 times: ")
        var holds = Calibration.Recorder()
        source.onInput = { input in
            holds.observe(input)
            _ = engine.handle(input)
        }
        PresenceCheck.runUntil(timeout: 15) { holds.pressDurations.count >= 6 }
        let holdVerdict = Calibration.analyzeHolds(
            holds.pressDurations, holdThresholdMs: settings.holdThresholdMs
        )
        print("  holds: \(holdVerdict.detail)")

        // Phase 2: doubles.
        PresenceCheck.waitEnter("Phase 2/3 — DOUBLE-TAP: press Enter, then double-tap Option 3 times: ")
        var taps = Calibration.Recorder()
        source.onInput = { input in
            taps.observe(input)
            _ = engine.handle(input)
        }
        PresenceCheck.runUntil(timeout: 15) { taps.tapGaps.count >= 4 }
        let tapVerdict = Calibration.analyzeTaps(
            presses: taps.pressDurations, gaps: taps.tapGaps,
            maxTapMs: settings.maxTapMs, windowMs: settings.doubleTapWindowMs
        )
        print("  taps: \(tapVerdict.detail)")

        // Phase 3: typing proof.
        print("Phase 3/3 — TYPING: type normally for 30 s.")
        print("  Use Option shortcuts, Option-click, accented characters (⌥e + vowel).")
        PresenceCheck.waitEnter("  Press Enter to start the 30 s clock: ")
        let before = engine.stats
        source.onInput = { input in _ = engine.handle(input) }
        PresenceCheck.runUntil(timeout: 30) { false }
        let after = engine.stats
        var delta = PresenceGesture.Stats()
        delta.inputs = after.inputs - before.inputs
        delta.activations = after.activations - before.activations
        delta.chordBlocks = after.chordBlocks - before.chordBlocks
        delta.taps = after.taps - before.taps
        let typingVerdict = Calibration.analyzeTyping(stats: delta)
        print("  typing: \(typingVerdict.detail)")

        let report = Calibration.buildReport(holds: holdVerdict, taps: tapVerdict, typing: typingVerdict)
        writeReport(report)
        print("")
        print(report.pass ? "PASS: gestures fit your hands." : "FAIL: see details above.")
        if json, let data = try? JSONEncoder().encode(report),
           let text = String(data: data, encoding: .utf8) {
            print(text)
        }
        if let command = report.enableCommand {
            print("Next: run this, then `presence-check hud`:")
            print("  \(command)")
        }
        return report.pass ? 0 : 1
    }

    private static func writeReport(_ report: Calibration.Report) {
        let url = URL(fileURLWithPath: (reportPath as NSString).expandingTildeInPath)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        if let data = try? JSONEncoder().encode(report) {
            try? data.write(to: url)
            print("Report: \(url.path)")
        }
    }
}
