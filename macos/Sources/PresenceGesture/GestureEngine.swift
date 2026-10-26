import Foundation

/// Option-key gesture engine: the no-window front door of Evie presence.
///
/// Pure Swift, no UI, no clock of its own: every input carries an explicit
/// millisecond timestamp, so tests drive the machine with virtual time and
/// the real adapter (`OptionGestureTap` in the EV target) stamps real time.
/// Time must be non-decreasing; time travel is clamped, never fatal.
///
/// Gestures:
/// - Option HOLD (>= holdThresholdMs, still down) → start listening; release → stop.
/// - Option DOUBLE-TAP (two taps within doubleTapWindowMs) → latch listening
///   until the next tap, Esc, or latchMaxMs (warning at latchWarnMs).
/// - Esc → cancel the turn (and stop listening if listening).
/// - A lone single tap does nothing by itself.
///
/// CHORD GUARD: any other key, mouse click, scroll, or extra modifier while
/// Option is down cancels the gesture until all modifiers release, so Option
/// shortcuts, Option-click, and special-character typing never trigger.
public enum PresenceGesture {

    // MARK: - Settings

    public enum OptionSide: String, Sendable, Equatable {
        case left, right
    }

    public enum OptionScope: String, Sendable, Equatable {
        case any, left, right

        public func allows(_ side: OptionSide) -> Bool {
            switch self {
            case .any: return true
            case .left: return side == .left
            case .right: return side == .right
            }
        }
    }

    public struct Settings: Sendable, Equatable {
        /// Master switch. Default OFF: the owner enables it via the guided
        /// check (D7) only. While off, every input is ignored.
        public var enabled: Bool = false
        public var optionScope: OptionScope = .any
        /// Press still down at this age becomes a hold. (~220 ms)
        public var holdThresholdMs: Int = 220
        /// A tap longer than this is not a tap. (250 ms; must exceed minTapMs)
        public var maxTapMs: Int = 250
        /// Presses shorter than this are switch bounce, ignored. (30 ms)
        public var minTapMs: Int = 30
        /// First release → second press window for a double-tap. (350 ms)
        public var doubleTapWindowMs: Int = 350
        /// Double-tap latch on/off. When off, double-taps do nothing.
        public var tapLatchEnabled: Bool = true
        /// Latch lifetime cap (10 min) and one-shot warning (9 min).
        public var latchMaxMs: Int = 600_000
        public var latchWarnMs: Int = 540_000

        public init() {}
    }

    // MARK: - Inputs (all timestamps in ms, monotonic)

    public enum Input: Sendable, Equatable {
        /// - exclusive=false means other modifiers were already held
        ///   (Cmd+Option…) → immediately chord-blocked.
        case optionDown(side: OptionSide, at: Int, exclusive: Bool)
        case optionUp(side: OptionSide, at: Int)
        case otherKey(at: Int)
        case mouseDown(at: Int)
        case scroll(at: Int)
        case extraModifier(at: Int)
        /// Every modifier is physically up (tap flags observation).
        case modifiersReleased(at: Int)
        case escape(at: Int)
        case tick(at: Int)
        /// Adapter-observed physical Option state (lost-event repair).
        case resync(optionPhysicalDown: Bool, at: Int)
        case setEnabled(Bool, at: Int)
        /// Secure event input started / tap disabled by the OS.
        case suspend(at: Int)
        case resume(at: Int)
    }

    // MARK: - Outputs

    public enum ListenReason: String, Sendable, Equatable {
        case hold, doubleTap
    }

    public enum StopReason: String, Sendable, Equatable {
        case released, tapped, cancelled, latchExpired, disabled, suspended
    }

    public enum Output: Sendable, Equatable {
        case startListening(ListenReason)
        case stopListening(StopReason)
        case cancelTurn
        case latchWarning(remainingMs: Int)
    }

    // MARK: - Stats (evidence for calibration + the zero-activation proof)

    public struct Stats: Sendable, Equatable {
        public var inputs: Int = 0
        public var activations: Int = 0
        public var stops: Int = 0
        public var taps: Int = 0
        public var bounces: Int = 0
        public var chordBlocks: Int = 0
        public var resyncs: Int = 0
        public var lostKeyUpRescues: Int = 0
        public var suspends: Int = 0
        public init() {}
    }
}

/// The gesture state machine. A value type: hosts hold one instance and feed
/// it inputs; every call returns the outputs to deliver, in order.
public struct GestureEngine: Sendable {
    public private(set) var settings: PresenceGesture.Settings
    public private(set) var stats = PresenceGesture.Stats()

    private enum State: Sendable, Equatable {
        case idle
        case suspended
        case down(since: Int, side: PresenceGesture.OptionSide)
        case listeningHold(since: Int)
        case tapPending(releaseAt: Int)
        case secondDown(since: Int, firstReleaseAt: Int)
        case latched(since: Int, warned: Bool)
        case unlatchDown(since: Int, latchSince: Int, warned: Bool)
        case chordBlocked(resumeLatchedSince: Int?, warned: Bool)
    }

    private var state: State = .idle
    private var lastTime: Int = 0

    public init(settings: PresenceGesture.Settings = .init()) {
        self.settings = settings
    }

    public var isListening: Bool {
        switch state {
        case .listeningHold, .latched, .unlatchDown: return true
        default: return false
        }
    }

    public var isLatched: Bool {
        switch state {
        case .latched, .unlatchDown: return true
        default: return false
        }
    }

    /// One-line state for HUD debug and calibration logs. Never user-facing.
    public var stateDescription: String {
        switch state {
        case .idle: return "idle"
        case .suspended: return "suspended"
        case .down: return "down"
        case .listeningHold: return "listening(hold)"
        case .tapPending: return "tap-pending"
        case .secondDown: return "second-down"
        case .latched: return "latched"
        case .unlatchDown: return "unlatch-down"
        case .chordBlocked: return "chord-blocked"
        }
    }

    // MARK: - Main entry

    @discardableResult
    public mutating func handle(_ input: PresenceGesture.Input) -> [PresenceGesture.Output] {
        stats.inputs += 1
        let now = clampedTime(input)
        var out: [PresenceGesture.Output] = []
        advanceClock(to: now, out: &out)

        // The master switch gates everything except itself.
        if case .setEnabled(let on, _) = input {
            applySetEnabled(on)
            out.append(contentsOf: pendingStop)
            pendingStop.removeAll()
            return out
        }
        guard settings.enabled else { return out }
        if case .suspended = state {
            if case .resume = input { state = .idle }
            return out
        }

        switch input {
        case .optionDown(let side, _, let exclusive):
            handleOptionDown(side: side, at: now, exclusive: exclusive, out: &out)
        case .optionUp(_, _):
            handleOptionUp(at: now, out: &out)
        case .otherKey, .mouseDown, .scroll, .extraModifier:
            handleChordInput(out: &out)
        case .modifiersReleased:
            handleModifiersReleased(at: now, out: &out)
        case .escape:
            handleEscape(out: &out)
        case .resync(let physicalDown, _):
            handleResync(physicalDown: physicalDown, at: now, out: &out)
        case .suspend:
            handleSuspend(out: &out)
        case .resume:
            break // not suspended (handled above); stray resume is a no-op
        case .tick, .setEnabled:
            break // clock already advanced; setEnabled handled above
        }
        return out
    }

    // Helper channel so applySetEnabled can emit a stop through handle().
    private var pendingStop: [PresenceGesture.Output] = []

    // MARK: - Clock

    private func clampedTime(_ input: PresenceGesture.Input) -> Int {
        let t: Int
        switch input {
        case .optionDown(_, let at, _): t = at
        case .optionUp(_, let at): t = at
        case .otherKey(let at): t = at
        case .mouseDown(let at): t = at
        case .scroll(let at): t = at
        case .extraModifier(let at): t = at
        case .modifiersReleased(let at): t = at
        case .escape(let at): t = at
        case .tick(let at): t = at
        case .resync(_, let at): t = at
        case .setEnabled(_, let at): t = at
        case .suspend(let at): t = at
        case .resume(let at): t = at
        }
        return max(t, lastTime)
    }

    /// Time-driven transitions evaluated before every input: hold fire,
    /// tap-pending expiry, latch warning/expiry. Idempotent per timestamp.
    private mutating func advanceClock(to now: Int, out: inout [PresenceGesture.Output]) {
        lastTime = now
        switch state {
        case .down(let since, _):
            if now - since >= settings.holdThresholdMs {
                state = .listeningHold(since: since)
                emit(.startListening(.hold), to: &out)
            }
        case .secondDown(let since, _):
            // Tap-then-hold is a hold, not a double-tap: the first tap is
            // consumed and this press behaves exactly like a first-press hold.
            if now - since >= settings.holdThresholdMs {
                state = .listeningHold(since: since)
                emit(.startListening(.hold), to: &out)
            }
        case .tapPending(let releaseAt):
            if now - releaseAt > settings.doubleTapWindowMs {
                state = .idle
            }
        case .latched(let since, let warned):
            let age = now - since
            if age >= settings.latchMaxMs {
                state = .idle
                emit(.stopListening(.latchExpired), to: &out)
            } else if !warned, age >= settings.latchWarnMs {
                state = .latched(since: since, warned: true)
                out.append(.latchWarning(remainingMs: settings.latchMaxMs - age))
            }
        default:
            break
        }
    }

    // MARK: - Inputs

    private mutating func handleOptionDown(
        side: PresenceGesture.OptionSide, at now: Int, exclusive: Bool,
        out: inout [PresenceGesture.Output]
    ) {
        switch state {
        case .idle:
            if !exclusive {
                blockChord(resumeLatchedSince: nil, out: &out)
            } else if !settings.optionScope.allows(side) {
                break // out-of-scope side: not our modifier at all
            } else {
                state = .down(since: now, side: side)
            }
        case .tapPending(let releaseAt):
            // Window already enforced by advanceClock; a live pending + a
            // second press is a double-tap candidate.
            if !exclusive {
                state = .idle
                blockChord(resumeLatchedSince: nil, out: &out)
            } else if !settings.optionScope.allows(side) {
                state = .idle // other side pressed: not a double-tap
            } else if !settings.tapLatchEnabled {
                state = .idle // latch off: second tap consumed, nothing fires
            } else {
                state = .secondDown(since: now, firstReleaseAt: releaseAt)
            }
        case .latched(let since, let warned):
            if settings.optionScope.allows(side) {
                state = .unlatchDown(since: now, latchSince: since, warned: warned)
            }
            // Out-of-scope side while latched: ignored, latch survives.
        case .down(_, let firstSide):
            if firstSide == side {
                break // duplicate down (lost up + repeat): stay down, no output
            }
            // Both Options at once is not a gesture.
            blockChord(resumeLatchedSince: nil, out: &out)
        case .secondDown, .unlatchDown:
            // A second Option press mid-press is not a gesture either.
            blockChord(resumeLatchedSince: unlatchLatchSince(), out: &out)
        case .listeningHold:
            // The other Option side pressed mid-hold: not a clean hold.
            emit(.stopListening(.cancelled), to: &out)
            blockChord(resumeLatchedSince: nil, out: &out)
        case .chordBlocked, .suspended:
            break
        }
    }

    private func unlatchLatchSince() -> Int? {
        if case .unlatchDown(_, let latchSince, _) = state { return latchSince }
        return nil
    }

    private func unlatchWarned() -> Bool {
        if case .unlatchDown(_, _, let warned) = state { return warned }
        return false
    }

    private mutating func handleOptionUp(at now: Int, out: inout [PresenceGesture.Output]) {
        switch state {
        case .down(let since, _):
            let dur = now - since
            if dur < settings.minTapMs {
                stats.bounces += 1
                state = .idle
            } else if dur <= settings.maxTapMs {
                // dur < holdThresholdMs is guaranteed: advanceClock would
                // have moved us to listeningHold otherwise.
                stats.taps += 1
                state = .tapPending(releaseAt: now)
            } else {
                // Between maxTap and hold (odd config): neither tap nor
                // hold — consume silently, never guess an activation.
                state = .idle
            }
        case .secondDown(let since, let firstReleaseAt):
            let dur = now - since
            if dur < settings.minTapMs {
                stats.bounces += 1
                state = .tapPending(releaseAt: firstReleaseAt)
            } else if dur <= settings.maxTapMs {
                state = .latched(since: now, warned: false)
                emit(.startListening(.doubleTap), to: &out)
            } else {
                state = .idle
            }
        case .listeningHold:
            state = .idle
            emit(.stopListening(.released), to: &out)
        case .unlatchDown:
            // Any complete press-release unlatches, however long the press.
            state = .idle
            emit(.stopListening(.tapped), to: &out)
        case .idle, .tapPending, .latched, .chordBlocked, .suspended:
            break // stray up: no output
        }
    }

    private mutating func handleChordInput(out: inout [PresenceGesture.Output]) {
        switch state {
        case .down, .secondDown:
            // Typing / clicking / scrolling with Option held: this press
            // was a shortcut or special character, never a gesture.
            blockChord(resumeLatchedSince: nil, out: &out)
        case .listeningHold:
            // Proof the hold was typing, not talking: stop, then block.
            emit(.stopListening(.cancelled), to: &out)
            blockChord(resumeLatchedSince: nil, out: &out)
        case .unlatchDown(_, let latchSince, let warned):
            // The unlatch press turned into typing: cancel the unlatch and
            // resume the latch once modifiers release.
            blockChord(resumeLatchedSince: latchSince, warned: warned, out: &out)
        case .tapPending:
            // A key/click/scroll between taps means "not a double-tap".
            state = .idle
        case .idle, .latched, .chordBlocked, .suspended:
            break // Option is up (or blocked): typing is just typing.
        }
    }

    private mutating func handleModifiersReleased(at now: Int, out: inout [PresenceGesture.Output]) {
        switch state {
        case .chordBlocked(let resumeSince, let warned):
            if let since = resumeSince {
                state = .latched(since: since, warned: warned)
            } else {
                state = .idle
            }
        case .down, .secondDown, .unlatchDown, .listeningHold:
            // We believed Option was down but the OS says every modifier
            // is up: the key-up was lost. Treat this as the release.
            stats.lostKeyUpRescues += 1
            handleOptionUp(at: now, out: &out)
        case .idle, .tapPending, .latched, .suspended:
            break
        }
    }

    private mutating func handleEscape(out: inout [PresenceGesture.Output]) {
        switch state {
        case .listeningHold:
            emit(.stopListening(.cancelled), to: &out)
            out.append(.cancelTurn)
            // Option is still down: suppress until release.
            blockChord(resumeLatchedSince: nil, out: &out)
        case .latched:
            emit(.stopListening(.cancelled), to: &out)
            out.append(.cancelTurn)
            state = .idle
        case .unlatchDown:
            emit(.stopListening(.cancelled), to: &out)
            out.append(.cancelTurn)
            blockChord(resumeLatchedSince: nil, out: &out)
        case .down, .secondDown:
            out.append(.cancelTurn)
            blockChord(resumeLatchedSince: nil, out: &out)
        case .tapPending, .idle, .chordBlocked, .suspended:
            out.append(.cancelTurn)
            if case .tapPending = state { state = .idle }
        }
    }

    private mutating func handleResync(
        physicalDown: Bool, at now: Int, out: inout [PresenceGesture.Output]
    ) {
        let believedDown: Bool
        switch state {
        case .down, .secondDown, .unlatchDown, .listeningHold: believedDown = true
        default: believedDown = false
        }
        if believedDown, !physicalDown {
            stats.resyncs += 1
            stats.lostKeyUpRescues += 1
            handleOptionUp(at: now, out: &out)
        } else if !believedDown, physicalDown {
            // A press we never saw the start of: adopt as blocked, never
            // activate on uncertain state.
            stats.resyncs += 1
            switch state {
            case .latched(let since, let warned):
                blockChord(resumeLatchedSince: since, warned: warned, out: &out)
            case .chordBlocked:
                break
            default:
                blockChord(resumeLatchedSince: nil, out: &out)
            }
        } else if case .chordBlocked(let resumeSince, let warned) = state, !physicalDown {
            // Blocked but nothing is held anymore: clear without activation.
            stats.resyncs += 1
            if let since = resumeSince {
                state = .latched(since: since, warned: warned)
            } else {
                state = .idle
            }
        }
    }

    private mutating func handleSuspend(out: inout [PresenceGesture.Output]) {
        stats.suspends += 1
        if isListening {
            emit(.stopListening(.suspended), to: &out)
        }
        state = .suspended
    }

    private mutating func applySetEnabled(_ on: Bool) {
        if on {
            settings.enabled = true
            state = .idle
        } else {
            settings.enabled = false
            if isListening {
                pendingStop.append(.stopListening(.disabled))
                stats.stops += 1
            }
            state = .idle
        }
    }

    // MARK: - Helpers

    private mutating func blockChord(
        resumeLatchedSince: Int?, warned: Bool = false,
        out: inout [PresenceGesture.Output]
    ) {
        _ = out
        stats.chordBlocks += 1
        state = .chordBlocked(resumeLatchedSince: resumeLatchedSince, warned: warned)
    }

    private mutating func emit(_ output: PresenceGesture.Output, to out: inout [PresenceGesture.Output]) {
        switch output {
        case .startListening: stats.activations += 1
        case .stopListening: stats.stops += 1
        case .cancelTurn, .latchWarning: break
        }
        out.append(output)
    }
}
