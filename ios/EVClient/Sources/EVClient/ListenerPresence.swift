import Foundation

/// EvieListenerPresenceEngine — listener feedback while the owner holds the floor.
///
/// A listener backchannel is NOT an assistant turn. It never creates a
/// provider response, commits conversational history, cancels user speech,
/// triggers barge-in, or transfers turn ownership. It is the vocal equivalent
/// of a human nod.
///
/// Timing is multi-signal and acoustic — never interval-driven. Opportunities
/// arise from speech-energy decay into bounded clause pauses; turn-end
/// likelihood suppresses cues where real turn-taking should happen. Content
/// selection is a separate stochastic, history-aware decision that defaults
/// to semantically safe neutral cues. Everything here is pure Foundation so
/// it is unit-testable without audio hardware.

// MARK: - Classes and variants

public enum ListenerFeedbackClass: String, Sendable, CaseIterable {
    /// mm / mhm / uh-huh — almost no semantic commitment. The default.
    case neutralContinuer
    /// yeah / okay / right — mild acknowledgment.
    case lightAcknowledgment
    /// I see / got it — comprehension signaled.
    case comprehension
    /// oh / ah — reactive.
    case affectiveReaction
    /// nice / good — positive endorsement; requires strong lexical evidence.
    case positiveFeedback

    /// Contextual confidence this class demands before it may be selected.
    public var requiredConfidence: Float {
        switch self {
        case .neutralContinuer: return 0
        case .lightAcknowledgment: return 0.35
        case .comprehension: return 0.45
        case .affectiveReaction: return 0.5
        case .positiveFeedback: return 0.7
        }
    }
}

/// Perceptual duration families. Longer ≠ louder: elongated clips ship at
/// LOWER gain so a warm "mhmmm…" stays clearly beneath the owner's floor.
public enum ListenerDurationFamily: String, Sendable, CaseIterable {
    case micro      // ~250–450 ms, very subtle
    case normal     // ~450–800 ms, default nod
    case elongated  // ~700–1200 ms, warm "mhmmm…" at strong continuation points
}

public struct ListenerVariant: Sendable {
    public var id: String
    public var kind: ListenerFeedbackClass
    public var family: ListenerDurationFamily
    /// Per-variant playback gain (asset-tuned, not a policy knob).
    public var gain: Float
    public var pcm16: Data
    public var sampleRate: Int

    public init(
        id: String,
        kind: ListenerFeedbackClass,
        pcm16: Data,
        sampleRate: Int = 16_000,
        family: ListenerDurationFamily = .normal,
        gain: Float = 0.34
    ) {
        self.id = id
        self.kind = kind
        self.family = family
        self.gain = gain
        self.pcm16 = pcm16
        self.sampleRate = sampleRate
    }
}

// MARK: - Features and opportunities

/// Acoustic evidence for one instant of the owner's turn (~20 ms frames).
public struct OwnerFloorFeatures: Sendable {
    public var frameIndex: UInt64
    /// RMS of this frame (linear 0…1 scale on int16 PCM).
    public var rms: Float
    /// Duration of this frame in ms (live mic delivers ~100 ms frames).
    public var frameMs: Int

    public init(frameIndex: UInt64, rms: Float, frameMs: Int) {
        self.frameIndex = frameIndex
        self.rms = rms
        self.frameMs = max(1, frameMs)
    }
}

/// A detected natural opening for listener feedback.
public struct BackchannelOpportunity: Sendable {
    public var atFrame: UInt64
    /// Continuous owner speech before this pause candidate, ms.
    public var speechMs: Int
    /// Pause length at the candidate point, ms.
    public var pauseMs: Int
    /// Energy ratio entering the pause (lower = clearer clause decay).
    public var entryDecayRatio: Float
    /// Estimated probability the owner is FINISHED (turn end), 0…1.
    public var turnEndProbability: Float
    /// Session-adapted pause cap used for this decision, ms.
    public var adaptedPauseMs: Int
    /// True when ASR partial activity was recently alive.
    public var partialActive: Bool

    public init(
        atFrame: UInt64,
        speechMs: Int,
        pauseMs: Int,
        entryDecayRatio: Float,
        turnEndProbability: Float,
        adaptedPauseMs: Int,
        partialActive: Bool
    ) {
        self.atFrame = atFrame
        self.speechMs = speechMs
        self.pauseMs = pauseMs
        self.entryDecayRatio = entryDecayRatio
        self.turnEndProbability = turnEndProbability
        self.adaptedPauseMs = adaptedPauseMs
        self.partialActive = partialActive
    }
}

public enum SuppressionReason: String, Sendable, CaseIterable {
    case disabled
    case shortTurn
    case tooSoon
    case turnEndLikely
    case recentBackchannel
    case lowConfidence
    case semanticRisk
    case assistantResponsePending
    case toolConfirmationState
    case audioRouteUnsafe
    case budgetSpent
    case noVariants
    case disfluencyGuard
    case sampleSuppressed
}

public enum ListenerDecision: Equatable, Sendable {
    case nothing(SuppressionReason)
    case visualOnly
    case vocal(kind: ListenerFeedbackClass, variantId: String)

    public var isVocal: Bool {
        if case .vocal = self { return true }
        return false
    }
}

// MARK: - Host context

public struct ListenerTurnContext: Sendable {
    /// Lowercased tail of the current stable partial transcript, if any.
    /// Optional — timing NEVER depends on this being present.
    public var partialTail: String?
    /// True while ASR partials have been arriving within the last second.
    public var partialActive: Bool
    /// True while a normal assistant response lifecycle is pending/running.
    public var assistantResponsePending: Bool
    /// True while a tool confirmation/approval hold is active.
    public var toolConfirmationActive: Bool
    /// True when the audio route is unsafe for overlap right now.
    public var audioRouteUnsafe: Bool
    /// Lexical/contextual gestures ("nice", "okay", "right", "I see").
    /// Recovery canary default is FALSE — nonlexical-only until audio
    /// ownership and floor behavior are owner-verified.
    public var semanticGesturesAllowed: Bool

    public init(
        partialTail: String? = nil,
        partialActive: Bool = false,
        assistantResponsePending: Bool = false,
        toolConfirmationActive: Bool = false,
        audioRouteUnsafe: Bool = false,
        semanticGesturesAllowed: Bool = false
    ) {
        self.partialTail = partialTail
        self.partialActive = partialActive
        self.assistantResponsePending = assistantResponsePending
        self.toolConfirmationActive = toolConfirmationActive
        self.audioRouteUnsafe = audioRouteUnsafe
        self.semanticGesturesAllowed = semanticGesturesAllowed
    }
}

// MARK: - Opportunity detection

/// Multi-signal acoustic detector for BACKCHANNEL RELEVANCE POINTS.
///
/// Fed one ``OwnerFloorFeatures`` per ~20 ms frame from the live mic path.
/// Emits at most one opportunity per pause episode, exactly when a bounded
/// clause pause opens after sustained owner speech with energy decaying into
/// it. Pure arithmetic per frame — allocation-free and safe to run beside the
/// existing barge-in detector. No wall-clock timers anywhere.
public final class BackchannelOpportunityDetector: @unchecked Sendable {
    public struct Config: Sendable {
        /// Owner must have held the floor this long before ANY cue opportunity.
        public var minSpeechMs: Int = 5_500
        /// Clause-pause window where a nod fits naturally.
        public var minPauseMs: Int = 170
        /// Fallback pause cap before a pause reads as turn-end/dead air.
        public var maxPauseMs: Int = 700
        /// Speech RMS gates with hysteresis.
        public var speechOnRms: Float = 0.012
        public var speechOffRms: Float = 0.007
        /// Sustained-speech ms between disfluency-streak cool-downs.
        public var disfluencyCooldownMs: Int = 900

        public init(
            minSpeechMs: Int = 5_500,
            minPauseMs: Int = 170,
            maxPauseMs: Int = 700,
            speechOnRms: Float = 0.012,
            speechOffRms: Float = 0.007,
            disfluencyCooldownMs: Int = 900
        ) {
            self.minSpeechMs = minSpeechMs
            self.minPauseMs = minPauseMs
            self.maxPauseMs = maxPauseMs
            self.speechOnRms = speechOnRms
            self.speechOffRms = speechOffRms
            self.disfluencyCooldownMs = disfluencyCooldownMs
        }
    }

    private let lock = NSLock()
    private var config = Config()
    private var frameIndex: UInt64 = 0
    private var speaking = false
    private var voicedFramesThisEpisode: UInt64 = 0
    private var speechEpisodeStart: UInt64 = 0
    private var hasSpeechEpisode = false
    private var pauseStart: UInt64?
    private var firedForPause = false
    private var recentPeakRms: Float = 0
    private var recentPausesMs: [Int] = []
    private var microBreakStreak = 0
    private var lastMicroBreakFrame: UInt64 = 0

    public init(config: Config = Config()) {
        self.config = config
    }

    /// New owner utterance: clear episode state, keep adaptation history.
    public func resetTurn() {
        lock.lock()
        speaking = false
        voicedFramesThisEpisode = 0
        speechEpisodeStart = 0
        hasSpeechEpisode = false
        pauseStart = nil
        firedForPause = false
        recentPeakRms = 0
        lock.unlock()
    }

    /// Frames are ~20 ms of 16 kHz mono PCM.
    public func ingest(_ f: OwnerFloorFeatures) -> BackchannelOpportunity? {
        lock.lock()
        defer { lock.unlock() }
        let ms = f.frameMs
        frameIndex = f.frameIndex

        if f.rms >= config.speechOnRms {
            speaking = true
            voicedFramesThisEpisode += 1
            if !hasSpeechEpisode {
                hasSpeechEpisode = true
                speechEpisodeStart = frameIndex
            }
            // Cool the hesitation guard during sustained fluent speech.
            if microBreakStreak > 0,
               frameIndex &- lastMicroBreakFrame > UInt64(max(1, config.disfluencyCooldownMs / ms)) {
                microBreakStreak -= 1
                lastMicroBreakFrame = frameIndex
            }
            if let start = pauseStart {
                let pauseMs = Int(frameIndex &- start) * ms
                if pauseMs >= config.minPauseMs {
                    recentPausesMs.append(pauseMs)
                    if recentPausesMs.count > 24 { recentPausesMs.removeFirst() }
                } else if pauseMs >= 60 {
                    microBreakStreak += 1
                    lastMicroBreakFrame = frameIndex
                }
                pauseStart = nil
            }
            firedForPause = false
            recentPeakRms = max(recentPeakRms * 0.94, f.rms)
            return nil
        }

        guard speaking || pauseStart != nil else { return nil }
        if speaking && f.rms < config.speechOffRms {
            speaking = false
            if pauseStart == nil { pauseStart = frameIndex }
            firedForPause = false
        }
        guard let pauseStartAt = pauseStart, !firedForPause else { return nil }

        let pauseMs = Int(frameIndex &- pauseStartAt) * ms
        guard pauseMs >= config.minPauseMs else { return nil }
        firedForPause = true

        let speechMs = Int(voicedFramesThisEpisode) * ms
        guard speechMs >= config.minSpeechMs else { return nil }
        guard microBreakStreak < 6 else { return nil }

        let adaptedCap = Self.adaptedMaxPause(recentPausesMs, fallback: config.maxPauseMs)
        let longness = Float(pauseMs) / Float(max(adaptedCap, 1))
        var turnEnd = min(1, longness * 0.8)
        if pauseMs > adaptedCap { turnEnd = max(turnEnd, 0.85) }
        if pauseMs < adaptedCap / 2 { turnEnd = min(turnEnd, 0.45) }
        let decay = min(1, f.rms / max(recentPeakRms, 1e-6))

        return BackchannelOpportunity(
            atFrame: frameIndex,
            speechMs: speechMs,
            pauseMs: pauseMs,
            entryDecayRatio: decay,
            turnEndProbability: turnEnd,
            adaptedPauseMs: adaptedCap,
            partialActive: false
        )
    }

    /// Session-local adaptation: the cap tracks near the upper quartile of
    /// THIS owner's own clause pauses, clamped to sane bounds.
    public static func adaptedMaxPause(_ pausesMs: [Int], fallback: Int) -> Int {
        guard pausesMs.count >= 5 else { return fallback }
        let sorted = pausesMs.sorted()
        let q3 = sorted[min(sorted.count - 1, (sorted.count * 3) / 4)]
        return min(950, max(340, q3))
    }
}

// MARK: - Policy

/// Stochastic, history-aware selection: SHOULD Evie signal, then WHAT fits.
/// Timing and content are separate concerns joined only here.
public final class BackchannelPolicy: @unchecked Sendable {
    public struct Config: Sendable {
        /// Master emit probability at a perfect vocal opportunity. Most valid
        /// opportunities intentionally stay silent or nod visually.
        public var emitScale: Float = 0.55
        /// Hard refractory floor between VOCAL cues (anti-spam guard only).
        public var refractoryFloorMs: Int = 7_000
        /// Randomized spacing above the floor; perceived spontaneity lives here.
        public var refractoryJitterMs: Int = 9_000
        /// Owner-speech ms before the first VOCAL cue may fire in a turn.
        public var firstCueAfterMs: Int = 8_000
        /// Conservative ceiling of vocal cues per owner turn.
        public var maxVocalPerTurn: Int = 3

        public init(
            emitScale: Float = 0.55,
            refractoryFloorMs: Int = 7_000,
            refractoryJitterMs: Int = 9_000,
            firstCueAfterMs: Int = 8_000,
            maxVocalPerTurn: Int = 3
        ) {
            self.emitScale = emitScale
            self.refractoryFloorMs = refractoryFloorMs
            self.refractoryJitterMs = refractoryJitterMs
            self.firstCueAfterMs = firstCueAfterMs
            self.maxVocalPerTurn = maxVocalPerTurn
        }
    }

    public struct History: Sendable {
        public var vocalThisTurn = 0
        public var visualThisTurn = 0
        public var lastVocalMs: Int = 0
        public var hasLastVocal = false
        public var lastVariantId: String?
        public var lastClass: ListenerFeedbackClass?

        public init() {}
    }

    public protocol RNG: Sendable {
        mutating func nextFloat() -> Float
    }

    /// Deterministic LCG for tests.
    public struct SeededRNG: RNG {
        private var state: UInt64
        public init(seed: UInt64) {
            state = seed &* 6364136223846793005 &+ 1442695040888963407
        }
        public mutating func nextFloat() -> Float {
            state = state &* 6364136223846793005 &+ 1442695040888963407
            return Float((state >> 33) & 0xFFFFFF) / Float(0xFFFFFF)
        }
    }

    /// Non-deterministic production randomness.
    public struct SystemRNG: RNG {
        public init() {}
        public mutating func nextFloat() -> Float { Float.random(in: 0...1) }
    }

    private let lock = NSLock()
    private var config = Config()
    private var rng: RNG
    private var history = History()

    public init(config: Config = Config(), rng: RNG = SystemRNG()) {
        self.config = config
        self.rng = rng
    }

    public func resetTurn() {
        lock.lock()
        history = History()
        lock.unlock()
    }

    public func noteVisualEmitted() {
        lock.lock()
        history.visualThisTurn += 1
        lock.unlock()
    }

    public func snapshotHistory() -> History {
        lock.lock()
        defer { lock.unlock() }
        return history
    }

    // MARK: semantic scans (lexical context → WHAT only, never WHEN)

    static let riskMarkers = [
        "don't", "do not", "dont", "never", "stop", "cancel", "delete",
        "remove", "wipe", "production", "deploy", "send ", "should we",
        "shall we", "maybe we", "?",
    ]

    static let positiveMarkers = [
        "worked", "fixed", "solved", "finally", "succeeded", "passed", "shipped",
    ]

    static let negativeMarkers = [
        "worried", "crash", "broken", "failed", "wrong", "angry", "stuck", "lost",
    ]

    public static func semanticRisk(_ tail: String?) -> Bool {
        guard let tail, !tail.isEmpty else { return false }
        let t = tail.lowercased()
        return riskMarkers.contains { t.contains($0) }
    }

    public static func positiveEvidence(_ tail: String?) -> Bool {
        guard let tail, !tail.isEmpty else { return false }
        let t = tail.lowercased()
        return positiveMarkers.contains { t.contains($0) }
    }

    public static func negativeEvidence(_ tail: String?) -> Bool {
        guard let tail, !tail.isEmpty else { return false }
        let t = tail.lowercased()
        return negativeMarkers.contains { t.contains($0) }
    }

    /// Decide at an opportunity. Deterministic given identical inputs+seed.
    public func decide(
        _ opp: BackchannelOpportunity,
        context: ListenerTurnContext,
        available: [ListenerVariant],
        nowMs: Int
    ) -> ListenerDecision {
        lock.lock()
        defer { lock.unlock() }

        func suppress(_ r: SuppressionReason) -> ListenerDecision { .nothing(r) }
        func visualFallback(_ r: SuppressionReason) -> ListenerDecision {
            history.visualThisTurn >= 5 ? suppress(r) : .visualOnly
        }

        if context.assistantResponsePending { return suppress(.assistantResponsePending) }
        if context.toolConfirmationActive { return suppress(.toolConfirmationState) }
        if context.audioRouteUnsafe { return suppress(.audioRouteUnsafe) }

        // TURN-END PROTECTION outranks everything: when the owner is probably
        // finished, wait for real conversational turn-taking.
        if opp.turnEndProbability >= 0.62 {
            return visualFallback(.turnEndLikely)
        }

        let risk = Self.semanticRisk(context.partialTail)

        // Budgets.
        if history.vocalThisTurn >= config.maxVocalPerTurn {
            return suppress(.budgetSpent)
        }

        // Refractory guard (hard floor + randomized jitter), vocal only.
        if history.hasLastVocal {
            let jitterMs = Int(rng.nextFloat() * Float(config.refractoryJitterMs))
            if nowMs - history.lastVocalMs < config.refractoryFloorMs + jitterMs {
                return visualFallback(.recentBackchannel)
            }
        } else if opp.speechMs < config.firstCueAfterMs {
            // Short/young utterances: silence, or at most a silent nod.
            return opp.speechMs >= 4_000 ? visualFallback(.shortTurn) : suppress(.shortTurn)
        }

        // Stochastic gate: most valid opportunities intentionally produce
        // nothing audible.
        let sample = rng.nextFloat()
        var emitProbability = config.emitScale
        if risk { emitProbability *= 0.45 }
        if opp.speechMs > 18_000 { emitProbability *= 1.15 }
        guard sample < emitProbability else { return visualFallback(.sampleSuppressed) }

        // ---- Content selection (separate concern) ----
        let candidates = available.filter { $0.pcm16.count > 0 }
        guard !candidates.isEmpty else { return suppress(.noVariants) }

        var wanted = ListenerFeedbackClass.neutralContinuer
        if !context.semanticGesturesAllowed {
            // SEMANTIC GESTURES DISABLED: recovery law — nonlexical-only.
            // Neutral continuers (mhm / mhmmm / hmm / mm-hm / uh-huh) remain.
            wanted = .neutralContinuer
        } else if Self.negativeEvidence(context.partialTail) || risk {
            // Sensitive or commitment-risky context: neutral or silence ONLY.
            wanted = .neutralContinuer
        } else if Self.positiveEvidence(context.partialTail), !context.partialTail.isNilOrEmpty {
            // Positive-result context exists AND acoustics say she's still
            // mid-story — a soft "nice" may fit.
            wanted = .positiveFeedback
        } else if opp.speechMs > 14_000 {
            wanted = .lightAcknowledgment
        }
        if opp.turnEndProbability > 0.45 && wanted != .neutralContinuer {
            wanted = .neutralContinuer
        }
        if confidence(for: wanted, context: context) < wanted.requiredConfidence {
            wanted = .neutralContinuer
        }

        var pool = candidates.filter { $0.kind == wanted }
        if pool.isEmpty { pool = candidates.filter { $0.kind == .neutralContinuer } }
        guard !pool.isEmpty else { return suppress(.noVariants) }

        // Never the exact same sound twice in a row.
        if pool.count > 1, let last = history.lastVariantId {
            pool.removeAll { $0.id == last }
        }
        guard !pool.isEmpty else { return suppress(.noVariants) }

        // DURATION FAMILIES ARE CONTEXTUAL. An elongated "mhmmm…" is a
        // stronger perceptual statement: reserve it for long-held floors,
        // high continuation likelihood, sparse recent feedback, and clear
        // distance from turn end. Otherwise default to normal/subtle.
        if pool.count > 1 {
            let wantElongated = opp.speechMs >= 12_000
                && opp.turnEndProbability <= 0.30
                && history.vocalThisTurn == 0
            let preferred = pool.filter {
                wantElongated ? $0.family == .elongated : $0.family != .elongated
            }
            if !preferred.isEmpty { pool = preferred }
        }
        let pick = pool[min(pool.count - 1, Int(rng.nextFloat() * Float(pool.count)))]

        history.vocalThisTurn += 1
        history.lastVariantId = pick.id
        history.lastClass = pick.kind
        history.lastVocalMs = nowMs
        history.hasLastVocal = true
        return .vocal(kind: pick.kind, variantId: pick.id)
    }

    /// Context confidence for a class. Acoustics alone are weak evidence;
    /// lexical markers raise it. Neutral always clears its zero bar.
    private func confidence(for kind: ListenerFeedbackClass, context: ListenerTurnContext) -> Float {
        switch kind {
        case .neutralContinuer: return 1
        case .lightAcknowledgment: return context.partialTail != nil ? 0.5 : 0.2
        case .comprehension: return 0.2
        case .affectiveReaction: return 0.2
        case .positiveFeedback:
            return Self.positiveEvidence(context.partialTail) ? 0.78 : 0.15
        }
    }
}

extension Optional where Wrapped == String {
    var isNilOrEmpty: Bool { self == nil || self!.isEmpty }
}

// MARK: - Conversational floor gate

/// HARD FLOOR AUTHORITY for listener feedback.
///
/// Listener gestures may be generated ONLY while the owner holds the floor.
/// Assistant response pending, assistant audio rendering, or the post-
/// playback speaker/room tail each make this return true — and when it is
/// true, opportunity generation must not run at all. Raw mic energy during
/// that window is SELF AUDIO, never owner speech.
public enum ListenerFloorGate {
    public static func isDormant(
        assistantResponsePending: Bool,
        responseLanePlaying: Bool,
        echoTailActive: Bool
    ) -> Bool {
        assistantResponsePending || responseLanePlaying || echoTailActive
    }
}

// MARK: - Telemetry

/// Structured listener-presence diagnostics. Numbers and codes only — never
/// transcript text. Rides the existing barge trace sink.
public enum ListenerTelemetry {
    public static func log(_ event: String, _ fields: [String: Any]) {
        BargeInTrace.log("listener.\(event)", fields)
    }
}
