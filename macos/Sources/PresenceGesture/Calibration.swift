import Foundation

/// Gesture calibration analysis (D7): record the owner's real Option press
/// timings, then judge the stock thresholds against them. Pure: the
/// interactive tool feeds live tap inputs; the checks feed synthetic ones.
///
/// The tool NEVER auto-tunes. It reports measured p50/max/gaps and PASSes
/// when the stock thresholds fit the owner's hands, else FAILs with the
/// exact `defaults write` lines to try — the owner stays in control.
public enum Calibration {

    // MARK: - Recorder

    /// Observes the input stream and extracts press durations + tap gaps.
    /// Chorded presses (shortcuts, accents) are excluded from hold/tap
    /// stats — they are typing, not gestures — but counted separately.
    public struct Recorder {
        public var pressDurations: [Int] = []
        public var tapGaps: [Int] = []
        public var chordedPresses = 0
        public var totalPresses = 0

        private var downAt: [PresenceGesture.OptionSide: Int] = [:]
        private var lastReleaseAt: Int?
        private var chordedSides = Set<PresenceGesture.OptionSide>()

        public init() {}

        public mutating func observe(_ input: PresenceGesture.Input) {
            switch input {
            case .optionDown(let side, let at, let exclusive):
                totalPresses += 1
                downAt[side] = at
                if !exclusive { chordedSides.insert(side) }
            case .optionUp(let side, let at):
                guard let since = downAt.removeValue(forKey: side) else { return }
                let duration = max(0, at - since)
                if chordedSides.contains(side) {
                    chordedSides.remove(side)
                    chordedPresses += 1
                    lastReleaseAt = nil
                    return
                }
                pressDurations.append(duration)
                if let last = lastReleaseAt {
                    tapGaps.append(max(0, since - last))
                }
                lastReleaseAt = at
            case .otherKey, .mouseDown, .scroll, .extraModifier:
                // Any chord while a side is down marks it chorded.
                for side in downAt.keys { chordedSides.insert(side) }
                lastReleaseAt = nil
            case .escape:
                lastReleaseAt = nil
            case .modifiersReleased, .tick, .resync, .setEnabled, .suspend, .resume:
                break
            }
        }
    }

    // MARK: - Verdicts

    public struct HoldVerdict: Codable, Equatable {
        public var samples: Int
        public var p50ms: Int
        public var maxMs: Int
        public var pass: Bool
        public var detail: String
    }

    public struct TapVerdict: Codable, Equatable {
        public var presses: Int
        public var gaps: Int
        public var slowestPressMs: Int
        public var widestGapMs: Int
        public var pass: Bool
        public var detail: String
    }

    public struct TypingVerdict: Codable, Equatable {
        public var activations: Int
        public var chordBlocks: Int
        public var taps: Int
        public var pass: Bool
        public var detail: String
    }

    public struct Report: Codable, Equatable {
        public var at: String
        public var holds: HoldVerdict
        public var taps: TapVerdict
        public var typing: TypingVerdict
        public var pass: Bool
        public var enableCommand: String?
    }

    public static let minHolds = 5
    public static let minDoubles = 3

    /// Holds PASS when at least `minHolds` clean presses were recorded and
    /// the typical hold clearly exceeds the threshold (p50 ≥ 1.2×).
    public static func analyzeHolds(
        _ durations: [Int], holdThresholdMs: Int
    ) -> HoldVerdict {
        let sorted = durations.sorted()
        guard sorted.count >= minHolds else {
            return HoldVerdict(
                samples: sorted.count, p50ms: percentile(sorted, 0.5) ?? 0,
                maxMs: sorted.max() ?? 0, pass: false,
                detail: "Only \(sorted.count) clean holds recorded (need \(minHolds)). Hold Option, release, repeat — no other keys."
            )
        }
        let p50 = percentile(sorted, 0.5) ?? 0
        let pass = p50 * 10 >= holdThresholdMs * 12
        return HoldVerdict(
            samples: sorted.count, p50ms: p50, maxMs: sorted.max() ?? 0,
            pass: pass,
            detail: pass
                ? "Typical hold \(p50) ms clears the \(holdThresholdMs) ms threshold."
                : "Typical hold \(p50) ms is too close to the \(holdThresholdMs) ms threshold — hold a beat longer, or lower EV_GESTURE_HOLD_MS."
        )
    }

    /// Doubles PASS when at least `minDoubles` gaps were recorded and every
    /// press/gap fits the tap window.
    public static func analyzeTaps(
        presses: [Int], gaps: [Int], maxTapMs: Int, windowMs: Int
    ) -> TapVerdict {
        let slowest = presses.max() ?? 0
        let widest = gaps.max() ?? 0
        guard gaps.count >= minDoubles else {
            return TapVerdict(
                presses: presses.count, gaps: gaps.count,
                slowestPressMs: slowest, widestGapMs: widest, pass: false,
                detail: "Only \(gaps.count) double-taps recorded (need \(minDoubles)). Tap-tap, pause, repeat."
            )
        }
        let pass = slowest <= maxTapMs && widest <= windowMs
        return TapVerdict(
            presses: presses.count, gaps: gaps.count,
            slowestPressMs: slowest, widestGapMs: widest, pass: pass,
            detail: pass
                ? "Slowest tap \(slowest) ms ≤ \(maxTapMs) ms; widest gap \(widest) ms ≤ \(windowMs) ms."
                : "Too slow (tap \(slowest) ms / gap \(widest) ms). Tap quicker, or raise the window."
        )
    }

    /// Typing PASSES only with zero activations AND proof the guard saw
    /// real Option typing (chord blocks > 0). Zero of everything means the
    /// owner didn't type — inconclusive, not a pass.
    public static func analyzeTyping(stats: PresenceGesture.Stats) -> TypingVerdict {
        if stats.activations > 0 {
            return TypingVerdict(
                activations: stats.activations, chordBlocks: stats.chordBlocks,
                taps: stats.taps, pass: false,
                detail: "\(stats.activations) accidental activation(s) while typing — do NOT enable gestures; file the report."
            )
        }
        if stats.chordBlocks == 0 {
            return TypingVerdict(
                activations: 0, chordBlocks: 0, taps: stats.taps, pass: false,
                detail: "No Option typing observed — type with Option shortcuts/accents and re-run."
            )
        }
        return TypingVerdict(
            activations: 0, chordBlocks: stats.chordBlocks, taps: stats.taps,
            pass: true,
            detail: "Zero activations across \(stats.inputs) events (\(stats.chordBlocks) chorded)."
        )
    }

    public static func buildReport(
        holds: HoldVerdict, taps: TapVerdict, typing: TypingVerdict,
        at: Date = Date()
    ) -> Report {
        let pass = holds.pass && taps.pass && typing.pass
        return Report(
            at: ISO8601DateFormatter().string(from: at),
            holds: holds, taps: taps, typing: typing, pass: pass,
            enableCommand: pass ? "defaults write com.ev.suit EV_GESTURE_ENABLED -bool true" : nil
        )
    }

    static func percentile(_ sorted: [Int], _ q: Double) -> Int? {
        guard !sorted.isEmpty else { return nil }
        let rank = q * Double(sorted.count - 1)
        let low = sorted[Int(floor(rank))]
        let high = sorted[Int(ceil(rank))]
        return Int((Double(low) + Double(high)) / 2)
    }
}
