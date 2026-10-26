import Foundation
import PresenceGesture

/// Calibration recorder + analyzer: synthetic inputs in, verdicts out.
/// The interactive flows (live tap, owner prompts, real panel) are
/// owner-run only and never execute here.
enum CalibrationChecks {
    typealias I = PresenceGesture.Input

    static func run(_ check: CheckFn) {
        // Recorder extracts clean press durations + tap gaps.
        do {
            var r = Calibration.Recorder()
            let inputs: [I] = [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 300),
                .optionDown(side: .left, at: 400, exclusive: true),
                .optionUp(side: .left, at: 460),
                // Chorded press: excluded from stats, counted separately.
                .optionDown(side: .left, at: 1000, exclusive: true),
                .otherKey(at: 1050),
                .optionUp(side: .left, at: 1100),
            ]
            for input in inputs { r.observe(input) }
            check(
                "calibration-recorder",
                r.pressDurations == [300, 60] && r.tapGaps == [100]
                    && r.chordedPresses == 1 && r.totalPresses == 3,
                "\(r.pressDurations) \(r.tapGaps)"
            )
        }
        // A key between taps breaks the gap chain (new double, not cont).
        do {
            var r = Calibration.Recorder()
            let gapInputs: [I] = [
                .optionDown(side: .left, at: 0, exclusive: true),
                .optionUp(side: .left, at: 60),
                .otherKey(at: 100),
                .optionDown(side: .left, at: 200, exclusive: true),
                .optionUp(side: .left, at: 260),
            ]
            for input in gapInputs { r.observe(input) }
            check(
                "calibration-gap-break",
                r.pressDurations == [60, 60] && r.tapGaps.isEmpty,
                "\(r.tapGaps)"
            )
        }
        // Holds: enough clean samples + p50 clears 1.2x threshold.
        do {
            let good = Calibration.analyzeHolds([300, 400, 350, 500, 320], holdThresholdMs: 220)
            check("calibration-holds-pass", good.pass && good.p50ms == 350, good.detail)
            let few = Calibration.analyzeHolds([300, 400], holdThresholdMs: 220)
            check("calibration-holds-few", !few.pass, few.detail)
            let quick = Calibration.analyzeHolds([200, 210, 205, 215, 208], holdThresholdMs: 220)
            check("calibration-holds-quick", !quick.pass, quick.detail)
        }
        // Doubles: 3+ gaps, every press/gap inside the window.
        do {
            let good = Calibration.analyzeTaps(
                presses: [90, 80, 95, 85], gaps: [150, 180, 160],
                maxTapMs: 250, windowMs: 350
            )
            check("calibration-taps-pass", good.pass, good.detail)
            let few = Calibration.analyzeTaps(presses: [90, 80], gaps: [150], maxTapMs: 250, windowMs: 350)
            check("calibration-taps-few", !few.pass, few.detail)
            let slow = Calibration.analyzeTaps(
                presses: [90, 300, 95, 85], gaps: [150, 180, 160],
                maxTapMs: 250, windowMs: 350
            )
            check("calibration-taps-slow-press", !slow.pass, slow.detail)
            let wide = Calibration.analyzeTaps(
                presses: [90, 80, 95, 85], gaps: [150, 900, 160],
                maxTapMs: 250, windowMs: 350
            )
            check("calibration-taps-wide-gap", !wide.pass, wide.detail)
        }
        // Typing: zero activations + observed Option typing, or no pass.
        do {
            var stats = PresenceGesture.Stats()
            stats.inputs = 2000
            stats.chordBlocks = 120
            stats.taps = 10
            let good = Calibration.analyzeTyping(stats: stats)
            check("calibration-typing-pass", good.pass, good.detail)
            var dirty = stats
            dirty.activations = 1
            let bad = Calibration.analyzeTyping(stats: dirty)
            check("calibration-typing-activation", !bad.pass, bad.detail)
            var idle = PresenceGesture.Stats()
            idle.inputs = 500
            let inconclusive = Calibration.analyzeTyping(stats: idle)
            check("calibration-typing-inconclusive", !inconclusive.pass, inconclusive.detail)
        }
        // Report: overall AND + enable command only on pass; Codable.
        do {
            let holds = Calibration.analyzeHolds([300, 400, 350, 500, 320], holdThresholdMs: 220)
            let taps = Calibration.analyzeTaps(
                presses: [90, 80, 95, 85], gaps: [150, 180, 160],
                maxTapMs: 250, windowMs: 350
            )
            var stats = PresenceGesture.Stats()
            stats.inputs = 100
            stats.chordBlocks = 5
            let typing = Calibration.analyzeTyping(stats: stats)
            let report = Calibration.buildReport(holds: holds, taps: taps, typing: typing)
            let encoded = (try? JSONEncoder().encode(report)) ?? Data()
            let decoded = try? JSONDecoder().decode(Calibration.Report.self, from: encoded)
            check(
                "calibration-report",
                report.pass && decoded == report
                    && (report.enableCommand?.contains("EV_GESTURE_ENABLED") == true),
                "pass=\(report.pass)"
            )
        }
    }
}
