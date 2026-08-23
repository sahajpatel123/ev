import Darwin
import Foundation

/// Local voice-turn owner for listen-while-speaking barge-in.
///
/// The microphone keeps capturing while Evie talks. Frames are *not*
/// forwarded to the provider until this session latches genuine near-end
/// speech. Playback stop happens here first; provider cancel is a follow-up.
public enum VoiceTurnPhase: String, Sendable {
    case idle
    case listening
    case userSpeaking
    case waitingForResponse
    case assistantSpeaking
    case interrupting
    case recovering
}

public struct PlaybackSnapshot: Sendable {
    public var pcm16: Data
    public var rms: Float
    public var audible: Bool
    public var echoGate: Bool
    public var playedMs: Int
    public var queuedMs: Int
    /// True while an assistant speaking episode is semantically recent —
    /// response pending/rendering OR provider chunks arrived within the gap
    /// tolerance. Streaming pauses drain buffers/rings WITHOUT ending the
    /// episode; turn decisions must not flip on that drain (P0 round four:
    /// the 42-second chop happened in exactly such a drain).
    public var assistantEpisodeActive: Bool

    public init(
        pcm16: Data = Data(),
        rms: Float = 0,
        audible: Bool = false,
        echoGate: Bool = false,
        playedMs: Int = 0,
        queuedMs: Int = 0,
        assistantEpisodeActive: Bool = false
    ) {
        self.pcm16 = pcm16
        self.rms = rms
        self.audible = audible
        self.echoGate = echoGate
        self.playedMs = playedMs
        self.queuedMs = queuedMs
        self.assistantEpisodeActive = assistantEpisodeActive
    }

    public static let silent = PlaybackSnapshot()
}

public struct BargeInDecision: Sendable {
    public var possibleSpeech: Bool
    public var confirmedUserSpeech: Bool
    public var confidence: Float
    public var onsetMs: Int
    public var candidateNs: UInt64
    public var confirmedNs: UInt64
    public var micRMS: Float
    public var playRMS: Float
    public var correlation: Float
    public var residualRMS: Float
    public var echoGain: Float
    public var persist: Int
    /// WHY a candidate failed to confirm (BI10). Empty when none/confirmed.
    public var rejectReason: String

    public static let empty = BargeInDecision(
        possibleSpeech: false,
        confirmedUserSpeech: false,
        confidence: 0,
        onsetMs: 0,
        candidateNs: 0,
        confirmedNs: 0,
        micRMS: 0,
        playRMS: 0,
        correlation: 0,
        residualRMS: 0,
        echoGain: 0,
        persist: 0,
        rejectReason: ""
    )
}

/// Structured barge-in telemetry. Numbers only — never PCM or transcripts.
///
/// I/O LAW (P0 2026-08-23 periodic-stall root cause): this logger is called
/// from the AVAudioEngine microphone tap thread. That engine ALSO renders
/// assistant audio on the same realtime I/O proc, so ANY synchronous work
/// here — file open/write/close, syslog — stalls output rendering and is
/// heard as mid-response silence. Logging therefore NEVER touches disk or
/// syslog synchronously: events land in an in-memory ring and a dedicated
/// background writer flushes them batched.
public enum BargeInTrace {
    public static let runtimeId = "ev-barge-runtime-v2"
    public static let engine = "local-detector-playback-authority"

    private static let lock = NSLock()
    private static var heartbeatNs: UInt64 = 0
    private static let path: URL = {
        let logs = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = logs.appendingPathComponent("Logs/EV", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("barge-trace.jsonl")
    }()
    /// Ring of pending serialized lines. Sized for ~30 s of storm-rate
    /// logging; overflow drops OLDEST lines rather than blocking callers.
    private static var pending: [Data] = []
    private static var pendingBytes = 0
    private static let pendingLimitBytes = 512_000
    private static let writerQueue = DispatchQueue(
        label: "ev.barge.trace.writer", qos: .utility
    )
    private static var writerScheduled = false

    public static func marker() {
        log("runtime", ["runtime": runtimeId, "engine": engine])
    }

    public static func heartbeatAllowed() -> Bool {
        let now = DispatchTime.now().uptimeNanoseconds
        lock.lock()
        defer { lock.unlock() }
        if now &- heartbeatNs < 200_000_000 { return false }
        heartbeatNs = now
        return true
    }

    public static func log(_ event: String, _ fields: [String: Any] = [:]) {
        var payload: [String: Any] = [
            "ts_ms": Int(Date().timeIntervalSince1970 * 1000),
            "mono_ns": DispatchTime.now().uptimeNanoseconds,
            "event": event,
            "runtime": runtimeId,
        ]
        for (key, value) in fields {
            payload[key] = value
        }
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload)
        else { return }
        var line = data
        line.append(0x0A)
        lock.lock()
        pending.append(line)
        pendingBytes += line.count
        let mustSchedule = !writerScheduled && !pending.isEmpty
        if mustSchedule { writerScheduled = true }
        // Backpressure: drop oldest half when a storm outpaces the writer.
        if pendingBytes > pendingLimitBytes {
            let drop = pending.count / 2
            for i in 0..<drop { pendingBytes -= pending[i].count }
            pending.removeFirst(drop)
        }
        lock.unlock()
        if mustSchedule {
            writerQueue.asyncAfter(deadline: .now() + 0.5, execute: flushPending)
        }
    }

    /// Background-only. Never called from an audio render/tap context.
    private static func flushPending() {
        lock.lock()
        let batch = pending
        pending.removeAll(keepingCapacity: true)
        pendingBytes = 0
        writerScheduled = false
        lock.unlock()
        guard !batch.isEmpty else { return }
        if !FileManager.default.fileExists(atPath: path.path) {
            FileManager.default.createFile(atPath: path.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: path) {
            defer { try? handle.close() }
            handle.seekToEndOfFile()
            for line in batch {
                try? handle.write(contentsOf: line)
            }
        }
    }
}

public struct BargeInInterrupt: Sendable {
    public var preroll: Data
    public var audioPlayedMs: Int
    public var confidence: Float
    public var prerollMs: Int
    public var candidateToConfirmedMs: Double
    public var confirmedToForwardMs: Double
}

/// Authoritative client phase for capture, playback, and interruption.
public final class VoiceTurnMachine: @unchecked Sendable {
    private let lock = NSLock()
    private var phase: VoiceTurnPhase = .listening
    private var interruptLatched = false
    private var outputGeneration: UInt64 = 0

    public init() {}

    public var currentPhase: VoiceTurnPhase {
        lock.lock()
        defer { lock.unlock() }
        return phase
    }

    public var currentOutputGeneration: UInt64 {
        lock.lock()
        defer { lock.unlock() }
        return outputGeneration
    }

    public func resetToListening() {
        lock.lock()
        phase = .listening
        interruptLatched = false
        lock.unlock()
    }

    public func noteMuted() {
        lock.lock()
        phase = .idle
        interruptLatched = false
        lock.unlock()
    }

    public func noteUserTranscript() {
        lock.lock()
        if phase == .interrupting {
            lock.unlock()
            return
        }
        interruptLatched = false
        phase = .waitingForResponse
        lock.unlock()
    }

    public func acceptAssistantChunk() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        switch phase {
        case .interrupting, .userSpeaking:
            return false
        case .assistantSpeaking:
            return true
        case .listening, .waitingForResponse, .recovering, .idle:
            phase = .assistantSpeaking
            interruptLatched = false
            outputGeneration &+= 1
            return true
        }
    }

    public func notePlaybackHeard() {
        lock.lock()
        switch phase {
        case .listening, .waitingForResponse, .recovering, .idle:
            phase = .assistantSpeaking
            interruptLatched = false
        default:
            break
        }
        lock.unlock()
    }

    public func notePlaybackEnded() {
        lock.lock()
        if phase == .assistantSpeaking {
            phase = .recovering
        }
        lock.unlock()
    }

    public func noteRecovered() {
        lock.lock()
        if phase == .recovering {
            phase = .listening
            interruptLatched = false
        }
        lock.unlock()
    }

    public func beginInterrupt() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard phase == .assistantSpeaking || phase == .recovering || phase == .listening else { return false }
        guard !interruptLatched else { return false }
        interruptLatched = true
        phase = .interrupting
        outputGeneration &+= 1
        return true
    }

    public func completeInterrupt() {
        lock.lock()
        if phase == .interrupting {
            phase = .userSpeaking
        }
        lock.unlock()
    }

    public func noteRemoteBargeIn() {
        lock.lock()
        interruptLatched = true
        if phase == .assistantSpeaking || phase == .recovering || phase == .interrupting {
            phase = .userSpeaking
        }
        outputGeneration &+= 1
        lock.unlock()
    }

    public func shouldRunDetector(playbackAudible: Bool = false) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        switch phase {
        case .interrupting, .userSpeaking, .idle:
            return false
        case .assistantSpeaking:
            return true
        case .recovering:
            // QUARANTINE: while recovering from assistant audio the detector
            // MUST stay armed — this is exactly the window where speaker tail
            // or provider-streaming gaps previously let self audio become a
            // fresh user turn.
            return true
        case .listening, .waitingForResponse:
            return playbackAudible
        }
    }

    public func canForwardMicToProvider(echoGate: Bool) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        switch phase {
        case .listening, .waitingForResponse:
            return !echoGate
        case .userSpeaking:
            return true
        case .idle, .assistantSpeaking, .interrupting, .recovering:
            return false
        }
    }
}

/// 16 kHz PCM16 ring used so barge-in confirmation does not drop the first word.
public final class MicPrerollBuffer: @unchecked Sendable {
    public let capacityBytes: Int
    public let sampleRate: Int
    private let lock = NSLock()
    private var storage = Data()

    public init(durationMs: Int = 400, sampleRate: Int = 16_000) {
        self.sampleRate = sampleRate
        self.capacityBytes = max(640, sampleRate * 2 * max(50, durationMs) / 1000)
        storage.reserveCapacity(capacityBytes)
    }

    public var durationMs: Int {
        capacityBytes * 1000 / max(1, sampleRate * 2)
    }

    public func append(_ pcm16: Data) {
        guard !pcm16.isEmpty else { return }
        lock.lock()
        storage.append(pcm16)
        if storage.count > capacityBytes {
            storage.removeFirst(storage.count - capacityBytes)
        }
        lock.unlock()
    }

    public func reset() {
        lock.lock()
        storage.removeAll(keepingCapacity: true)
        lock.unlock()
    }

    public func snapshot(fromOnsetMs onsetMs: Int, padMs: Int = 60) -> Data {
        lock.lock()
        let copy = storage
        lock.unlock()
        let bytesPerMs = max(1, sampleRate * 2 / 1000)
        let keepMs = min(durationMs, max(onsetMs + padMs, padMs))
        let keepBytes = min(copy.count, keepMs * bytesPerMs)
        let aligned = keepBytes - (keepBytes % 2)
        guard aligned > 0, aligned <= copy.count else { return copy }
        return copy.suffix(aligned)
    }
}

/// Echo-aware near-end speech detector.
///
/// Combines mic energy, speech-like zero-crossings, persistence, playback
/// RMS, and a cheap correlation against Evie's own recent PCM. A loud
/// speaker echo should not confirm; a human "wait" should.
public final class BargeInDetector: @unchecked Sendable {
    public static let sampleRate = 16_000
    public static let frameSamples = 320

    public struct Config: Sendable {
        public var speechFloor: Float = 0.010
        public var softSpeechFloor: Float = 0.0065
        public var playbackSilentRMS: Float = 0.004
        // ---- BARGE-IN V2: evidence fusion, session-adaptive calibration ----
        // Fixed RMS floors are fundamentally wrong (measured self-audio varies
        // 0.003–0.028 across sessions/rooms/volumes; real "Wait" measured
        // 0.016–0.13). V2 calibrates THIS session's echo floor from natural
        // assistant playback and demands INDEPENDENT near-end structure:
        // waveform matched-filter residual + persistence, not amplitude.
        /// Sanity minimum to become a candidate at all.
        public var absMinCandidate: Float = 0.012
        /// Sanity minimum to confirm even in a whisper-quiet room.
        public var absMinConfirm: Float = 0.016
        /// Candidate level = max(absMinCandidate, candidateFactor × echoEMA).
        public var candidateEchoFactor: Float = 2.2
        /// Confirm level = max(absMinConfirm, confirmFactor × echoEMA).
        public var confirmEchoFactor: Float = 3.0
        /// Matched-filter veto: echo-explained when best-fit residual ≤ this
        /// fraction of mic RMS (and fit gain is physically plausible).
        public var matchResidualRatio: Float = 0.20
        /// Plausible speaker→mic gain window for the matched filter.
        public var matchGainMin: Float = 0.08
        public var matchGainMax: Float = 1.3
        /// Legacy fixed-floor path when V2 is disabled.
        @available(*, deprecated, message: "legacy V1 gate")
        public var legacyNearEndRmsDuringPlayback: Float = 0.05
        public var minNearEndRmsDuringPlayback: Float = 0.05
        public var nearEndReferenceRatio: Float = 0.35
        public var echoCorrelation: Float = 0.52
        /// BARGE-IN V2 ROLLBACK FLAG. OFF (baseline default): the proven
        /// round-four fixed-floor episode gate `max(0.05, playRMS×0.35)` is
        /// active — it kept long assistant responses continuous (owner-verified
        /// >30 s) and is the safe fallback while V2 is tuned. ON enables the
        /// evidence-fusion near-end ownership path (matched-filter + session
        /// calibration) via `defaults write com.ev.suit EV_BARGE_IN_V2_ENABLED -bool YES`.
        public var v2EpisodeGate: Bool = {
            #if os(macOS) || os(iOS)
            if UserDefaults.standard.object(forKey: "EV_BARGE_IN_V2_ENABLED") != nil {
                return UserDefaults.standard.bool(forKey: "EV_BARGE_IN_V2_ENABLED")
            }
            #endif
            return false
        }()
        public var confirmFrames: Int = 5
        public var fastConfirmFrames: Int = 3
        public var softConfirmFrames: Int = 8
        public var maxCrest: Float = 14
        public var minZcr: Float = 0.018
        public var maxZcr: Float = 0.32
        public var echoGainInit: Float = 0.18
        public var echoGainMin: Float = 0.04
        public var echoGainMax: Float = 0.85

        public init() {}
    }

    private let lock = NSLock()
    private let config: Config
    private var pendingMic = Data()
    private var playbackFloat: [Float] = []
    private let playbackKeep = 16_000 / 1000 * 320
    private var persist = 0
    private var echoGain: Float
    private var candidateNs: UInt64 = 0
    private var onsetFrames = 0
    /// Session-adaptive calibration (V2). selfEchoEma tracks how loud Evie's
    /// playback leaks into THIS mic in THIS room; updated ONLY from frames
    /// with no candidate, so owner speech never contaminates the estimate.
    private var selfEchoEma: Float = 0.004
    private var recentMicRms: [Float] = []
    public private(set) var lastDecision = BargeInDecision.empty

    public init(config: Config = Config()) {
        self.config = config
        self.echoGain = config.echoGainInit
        playbackFloat.reserveCapacity(playbackKeep)
    }

    public func reset() {
        lock.lock()
        pendingMic.removeAll(keepingCapacity: true)
        playbackFloat.removeAll(keepingCapacity: true)
        persist = 0
        echoGain = config.echoGainInit
        candidateNs = 0
        onsetFrames = 0
        lastDecision = .empty
        lock.unlock()
    }

    public func analyze(microphonePCM16: Data, playback: PlaybackSnapshot) -> BargeInDecision {
        lock.lock()
        defer { lock.unlock() }
        appendPlayback(playback.pcm16)
        pendingMic.append(microphonePCM16)
        var decision = lastDecision
        decision.confirmedUserSpeech = false
        let frameBytes = Self.frameSamples * 2
        while pendingMic.count >= frameBytes {
            let frame = Data(pendingMic.prefix(frameBytes))
            pendingMic.removeFirst(frameBytes)
            decision = analyzeFrameLocked(
                frame,
                playbackRMS: playback.rms,
                playbackAudible: playback.audible,
                assistantEpisodeActive: playback.assistantEpisodeActive
            )
            lastDecision = decision
            if decision.confirmedUserSpeech {
                pendingMic.removeAll(keepingCapacity: true)
                persist = 0
                return decision
            }
        }
        lastDecision = decision
        return decision
    }

    private func analyzeFrameLocked(
        _ pcm16: Data,
        playbackRMS: Float,
        playbackAudible: Bool,
        assistantEpisodeActive: Bool = false
    ) -> BargeInDecision {
        let samples = Self.floatSamples(pcm16)
        let micRMS = Self.rms(samples)
        let zcr = Self.zeroCrossingRate(samples)
        let crest = Self.crest(samples, rms: micRMS)
        let playRMS = max(playbackRMS, Self.rms(Array(playbackFloat.suffix(Self.frameSamples))))
        let corr = maxAbsCorrelation(samples)
        let echoEstimate = playRMS * echoGain
        let residual = max(0, micRMS - echoEstimate * 0.85)
        if corr > config.echoCorrelation,
           playRMS > config.playbackSilentRMS,
           residual <= max(echoEstimate * 1.25, config.speechFloor) {
            let observed = micRMS / max(playRMS, 1e-6)
            echoGain = min(config.echoGainMax, max(config.echoGainMin, echoGain * 0.9 + observed * 0.1))
        }
        let speechLike = zcr >= config.minZcr && zcr <= config.maxZcr && crest < config.maxCrest

        // ================= BARGE-IN V2: EVIDENCE FUSION =================
        // Question: is there INDEPENDENT near-end human speech that Evie's
        // playback cannot explain? NOT "is mic RMS above a fixed number".
        //
        // Stage 1 (candidate, sensitive): level above session-calibrated
        // echo floor + speech-like structure.
        // Stage 2 (confirm): the candidate must survive a delay-aware
        // waveform matched filter — if the best-fitting delayed copy of
        // Evie's own audio explains the frame, it is SELF. Whatever remains
        // after subtracting that fit is the OWNER component.
        var rejectReason = ""
        var strong = false
        var soft = false

        // Maintain recent-mic history for onset/persistence context.
        recentMicRms.append(micRMS)
        if recentMicRms.count > 6 {
            recentMicRms.removeFirst(recentMicRms.count - 6)
        }

        let useV2EpisodeGate = config.v2EpisodeGate
        if useV2EpisodeGate, assistantEpisodeActive || playbackAudible {
            let candidateLevel = max(config.absMinCandidate, config.candidateEchoFactor * selfEchoEma)
            guard micRMS >= candidateLevel else {
                // Calibrate ONLY from non-candidate episode frames, so owner
                // speech can never contaminate the echo estimate.
                selfEchoEma = max(0.0015, selfEchoEma * 0.96 + micRMS * 0.04)
                persist = 0
                onsetFrames = 0
                candidateNs = 0
                let decision = BargeInDecision(
                    possibleSpeech: false, confirmedUserSpeech: false,
                    confidence: 0, onsetMs: 0, candidateNs: 0, confirmedNs: 0,
                    micRMS: micRMS, playRMS: playRMS, correlation: corr,
                    residualRMS: residual, echoGain: echoGain, persist: 0,
                    rejectReason: micRMS < config.absMinCandidate
                        ? "BELOW_ABSOLUTE_LEVEL"
                        : "BELOW_CALIBRATED_ECHO_FLOOR"
                )
                lastDecision = decision
                return decision
            }
            guard speechLike else {
                persist = 0
                onsetFrames = 0
                candidateNs = 0
                let decision = BargeInDecision(
                    possibleSpeech: false, confirmedUserSpeech: false,
                    confidence: 0, onsetMs: 0, candidateNs: 0, confirmedNs: 0,
                    micRMS: micRMS, playRMS: playRMS, correlation: corr,
                    residualRMS: residual, echoGain: echoGain, persist: 0,
                    rejectReason: "NOT_SPEECHLIKE"
                )
                lastDecision = decision
                return decision
            }

            // STAGE 2a — waveform matched-filter echo explanation.
            // If the best delayed+gain-fitted copy of Evie's own audio
            // explains this frame, it is SELF, however loud it is.
            var echoExplained = false
            let match = bestMatchedResidual(samples)
            if let m = match,
               m.gain >= config.matchGainMin, m.gain <= config.matchGainMax {
                echoExplained = m.residual <= max(0.0045, config.matchResidualRatio * micRMS)
            }
            if echoExplained {
                selfEchoEma = max(0.0015, selfEchoEma * 0.94 + micRMS * 0.06)
                persist = 0
                onsetFrames = 0
                candidateNs = 0
                let decision = BargeInDecision(
                    possibleSpeech: false, confirmedUserSpeech: false,
                    confidence: 0, onsetMs: 0, candidateNs: 0, confirmedNs: 0,
                    micRMS: micRMS, playRMS: playRMS, correlation: corr,
                    residualRMS: residual, echoGain: echoGain, persist: 0,
                    rejectReason: "HIGH_ECHO_MATCH"
                )
                lastDecision = decision
                return decision
            }
            // When NO structural evidence is available (reference too sparse
            // for matched filtering — provider-gap drains), fall back to the
            // CALIBRATED confirm floor rather than the bare candidate floor.
            if match == nil {
                let confirmLevel = max(config.absMinConfirm, config.confirmEchoFactor * selfEchoEma)
                guard micRMS >= confirmLevel else {
                    persist = 0
                    onsetFrames = 0
                    candidateNs = 0
                    let decision = BargeInDecision(
                        possibleSpeech: false, confirmedUserSpeech: false,
                        confidence: 0, onsetMs: 0, candidateNs: 0, confirmedNs: 0,
                        micRMS: micRMS, playRMS: playRMS, correlation: corr,
                        residualRMS: residual, echoGain: echoGain, persist: 0,
                        rejectReason: "BELOW_CALIBRATED_CONFIRM_FLOOR"
                    )
                    lastDecision = decision
                    return decision
                }
            }

            // STAGE 2b — persistence. Short words need only ~40 ms of
            // consecutive voiced frames; isolated room/desk transients never
            // accumulate two consecutive frames.
            persist += 1
            onsetFrames += 1
            if candidateNs == 0 {
                candidateNs = DispatchTime.now().uptimeNanoseconds
            }
            guard persist >= 2 else {
                let decision = BargeInDecision(
                    possibleSpeech: true, confirmedUserSpeech: false,
                    confidence: min(0.6, residual * 3), onsetMs: onsetFrames * 20,
                    candidateNs: candidateNs, confirmedNs: 0,
                    micRMS: micRMS, playRMS: playRMS, correlation: corr,
                    residualRMS: residual, echoGain: echoGain, persist: persist,
                    rejectReason: "INSUFFICIENT_PERSISTENCE"
                )
                lastDecision = decision
                return decision
            }
            strong = true
        } else {
            // Non-episode path unchanged (owner floor / quiet room).
            let headphones = !playbackAudible || playRMS < config.playbackSilentRMS
            let independent = residual >= max(config.speechFloor * 1.35, echoEstimate * 1.6)
            strong = residual >= config.speechFloor && speechLike && (headphones || corr < config.echoCorrelation || independent)
            soft = residual >= config.softSpeechFloor && speechLike && (headphones || corr < (config.echoCorrelation - 0.08) || independent)
            if !(strong || soft) {
                rejectReason = residual < config.speechFloor ? "INSUFFICIENT_RESIDUAL" : "ANOTHER_REASON"
            }
        }
        let possible = strong || soft
        var confirmed = false
        var confidence: Float = 0
        var headphones = false
        var independent = false
        if assistantEpisodeActive || playbackAudible {
            if useV2EpisodeGate {
                // V2 episode path: persistence (≥2 frames) already enforced
                // above; strong == confirmed.
                confirmed = strong
                if confirmed {
                    confidence = min(1, 0.5 + residual * 4)
                } else if possible {
                    confidence = min(0.6, residual * 3)
                }
            } else {
                // LEGACY V1 ROLLBACK: fixed near-end floor
                // max(0.05, playRMS×0.35). Known to reject normal-volume
                // "Wait" (measured 0.016–0.063); rollback only.
                let nearEndRequired = max(
                    config.minNearEndRmsDuringPlayback,
                    playRMS * config.nearEndReferenceRatio
                )
                if micRMS >= nearEndRequired && residual >= config.speechFloor && speechLike {
                    persist += 1
                } else {
                    persist = 0
                }
                confirmed = persist >= config.fastConfirmFrames
                confidence = confirmed ? min(1, 0.5 + residual * 4) : 0
            }
        } else {
            headphones = !playbackAudible || playRMS < config.playbackSilentRMS
            independent = residual >= max(config.speechFloor * 1.35, echoEstimate * 1.6)
            if possible {
                persist += 1
                onsetFrames += 1
                if candidateNs == 0 {
                    candidateNs = DispatchTime.now().uptimeNanoseconds
                }
            } else {
                persist = 0
                onsetFrames = 0
                candidateNs = 0
            }
            confirmed = (independent && persist >= config.fastConfirmFrames)
                || (strong && persist >= config.confirmFrames)
                || (soft && persist >= config.softConfirmFrames)
            if confirmed {
                confidence = min(1, 0.35 + residual * 4 + (headphones ? 0.25 : 0) + max(0, 0.4 - corr))
            } else if possible {
                confidence = min(0.6, residual * 3)
            }
        }
        let now = DispatchTime.now().uptimeNanoseconds
        let onsetMs = onsetFrames * 20
        let decision = BargeInDecision(
            possibleSpeech: possible,
            confirmedUserSpeech: confirmed,
            confidence: confidence,
            onsetMs: onsetMs,
            candidateNs: candidateNs,
            confirmedNs: confirmed ? now : 0,
            micRMS: micRMS,
            playRMS: playRMS,
            correlation: corr,
            residualRMS: residual,
            echoGain: echoGain,
            persist: persist,
            rejectReason: confirmed ? "" : rejectReason
        )
        if confirmed {
            persist = 0
            onsetFrames = 0
        }
        return decision
    }

    /// Delay-aware waveform matched filter: find the delayed slice of Evie's
    /// recent playback that best explains the current mic frame, fit the
    /// gain in closed form, and report what remains. High remaining energy
    /// means the frame contains something playback cannot explain — the
    /// OWNER component. (BI05/BI06 with real delay search.)
    private func bestMatchedResidual(_ frame: [Float]) -> (residual: Float, gain: Float)? {
        guard !playbackFloat.isEmpty, frame.count >= 64 else { return nil }
        var best: (residual: Float, gain: Float)? = nil
        for lag in [0, 80, 160, 320, 640, 960, 1600, 2400, 3200] {
            let end = playbackFloat.count - lag
            guard end >= frame.count else { continue }
            let start = end - frame.count
            let ref = Array(playbackFloat[start..<end])
            var refEnergy: Float = 0
            var dot: Float = 0
            for i in 0..<frame.count {
                refEnergy += ref[i] * ref[i]
                dot += frame[i] * ref[i]
            }
            guard refEnergy > 1e-7 else { continue }
            let gain = max(0, min(1.5, dot / refEnergy))
            var residualEnergy: Float = 0
            for i in 0..<frame.count {
                let diff = frame[i] - gain * ref[i]
                residualEnergy += diff * diff
            }
            let residual = (residualEnergy / Float(frame.count)).squareRoot()
            if best == nil || residual < best!.residual {
                best = (residual, gain)
            }
        }
        return best
    }

    private func appendPlayback(_ pcm16: Data) {
        guard !pcm16.isEmpty else { return }
        let extra = Self.floatSamples(pcm16)
        playbackFloat.append(contentsOf: extra)
        if playbackFloat.count > playbackKeep {
            playbackFloat.removeFirst(playbackFloat.count - playbackKeep)
        }
    }

    private func maxAbsCorrelation(_ mic: [Float]) -> Float {
        guard !playbackFloat.isEmpty, mic.count >= 64 else { return 0 }
        let lags = [0, 80, 160, 320, 640, 960, 1280, 1920, 2560, 3200, 4000]
        var best: Float = 0
        for lag in lags {
            guard playbackFloat.count > lag + 64 else { continue }
            let start = max(0, playbackFloat.count - lag - mic.count)
            let end = min(playbackFloat.count - lag, start + mic.count)
            if end - start < 64 { continue }
            let slice = Array(playbackFloat[start..<end])
            let value = abs(Self.normalizedCorrelation(mic, slice))
            if value > best { best = value }
        }
        return best
    }

    public static func floatSamples(_ pcm16: Data) -> [Float] {
        let count = pcm16.count / 2
        guard count > 0 else { return [] }
        var samples = [Float](repeating: 0, count: count)
        pcm16.withUnsafeBytes { raw in
            let src = raw.bindMemory(to: Int16.self)
            let n = min(count, src.count)
            for i in 0..<n {
                samples[i] = Float(src[i].littleEndian) / 32768.0
            }
        }
        return samples
    }

    public static func rms(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else { return 0 }
        var sum: Float = 0
        for sample in samples {
            sum += sample * sample
        }
        return sqrt(sum / Float(samples.count))
    }

    static func zeroCrossingRate(_ samples: [Float]) -> Float {
        guard samples.count > 1 else { return 0 }
        var crossings = 0
        for i in 1..<samples.count {
            if samples[i - 1] == 0 { continue }
            if samples[i] == 0 { continue }
            if (samples[i - 1] > 0) != (samples[i] > 0) {
                crossings += 1
            }
        }
        return Float(crossings) / Float(samples.count - 1)
    }

    static func crest(_ samples: [Float], rms: Float) -> Float {
        guard rms > 1e-8 else { return 0 }
        var peak: Float = 0
        for sample in samples {
            let abs = Swift.abs(sample)
            if abs > peak { peak = abs }
        }
        return peak / rms
    }

    static func normalizedCorrelation(_ a: [Float], _ b: [Float]) -> Float {
        let n = min(a.count, b.count)
        guard n >= 32 else { return 0 }
        var sumA: Float = 0
        var sumB: Float = 0
        var sumAA: Float = 0
        var sumBB: Float = 0
        var sumAB: Float = 0
        for i in 0..<n {
            let x = a[i]
            let y = b[i]
            sumA += x
            sumB += y
            sumAA += x * x
            sumBB += y * y
            sumAB += x * y
        }
        let nf = Float(n)
        let cov = sumAB - sumA * sumB / nf
        let va = sumAA - sumA * sumA / nf
        let vb = sumBB - sumB * sumB / nf
        let den = sqrt(max(va, 0) * max(vb, 0))
        guard den > 1e-12 else { return 0 }
        return max(-1, min(1, cov / den))
    }
}

/// Client-side barge-in owner: detector + preroll + phase + latch.
public final class LiveBargeInSession: @unchecked Sendable {
    public let machine = VoiceTurnMachine()
    public let detector = BargeInDetector()
    public let preroll = MicPrerollBuffer()
    public private(set) var lastInterrupt: BargeInInterrupt?
    /// SELF-PLAYBACK QUARANTINE: recovering → listening requires SUSTAINED
    /// mic quiet (room tail decayed) — not merely an empty playback queue.
    /// Any voiced frame resets the decay clock; confirmed near-end speech
    /// exits quarantine through the interrupt path instead.
    public var quarantineQuietSeconds: Double = 0.4
    private let lock = NSLock()
    private var quietSeconds: Double = 0

    public init() {}

    public func reset() {
        machine.resetToListening()
        detector.reset()
        preroll.reset()
        lastInterrupt = nil
        lock.lock()
        quietSeconds = 0
        lock.unlock()
    }

    public func handleMicFrame(
        _ pcm: Data,
        playback: PlaybackSnapshot,
        forward: (Data) -> Void,
        interrupt: (BargeInInterrupt) -> Void
    ) {
        if playback.audible {
            machine.notePlaybackHeard()
        }
        // QUARANTINE RELEASE: only sustained quiet (or a confirmed near-end
        // interrupt below) may open the owner-forwarding floor again.
        if machine.currentPhase == .recovering, !playback.audible, !playback.echoGate {
            let seconds = Double(pcm.count / 2) / Double(BargeInDetector.sampleRate)
            let rms = BargeInDetector.rms(BargeInDetector.floatSamples(pcm))
            lock.lock()
            if rms < 0.0065 {
                quietSeconds += seconds
            } else {
                quietSeconds = 0
            }
            let decayed = quietSeconds >= quarantineQuietSeconds
            lock.unlock()
            if decayed {
                machine.noteRecovered()
            }
        }
        preroll.append(pcm)
        let monitor = machine.shouldRunDetector(playbackAudible: playback.audible)
        if monitor {
            let decision = detector.analyze(microphonePCM16: pcm, playback: playback)
            if decision.possibleSpeech {
                BargeInTrace.log("detector.candidate", [
                    "confidence": Double(decision.confidence),
                    "mic_rms": Double(decision.micRMS),
                    "play_rms": Double(decision.playRMS),
                    "corr": Double(decision.correlation),
                    "residual": Double(decision.residualRMS),
                    "echo_gain": Double(decision.echoGain),
                    "persist": decision.persist,
                    "phase": machine.currentPhase.rawValue,
                ])
            } else if BargeInTrace.heartbeatAllowed() {
                BargeInTrace.log("capture.heartbeat", [
                    "mic_bytes": pcm.count,
                    "mic_rms": Double(decision.micRMS),
                    "play_rms": Double(decision.playRMS),
                    "corr": Double(decision.correlation),
                    "residual": Double(decision.residualRMS),
                    "echo_gain": Double(decision.echoGain),
                    "preroll_ms": preroll.snapshot(fromOnsetMs: 400).count * 1000 / max(1, 32_000),
                    "audible": playback.audible,
                    "echo_gate": playback.echoGate,
                    "phase": machine.currentPhase.rawValue,
                    "played_ms": playback.playedMs,
                ])
            }
            if decision.confirmedUserSpeech, machine.beginInterrupt() {
                BargeInTrace.log("detector.confirmed", [
                    "confidence": Double(decision.confidence),
                    "mic_rms": Double(decision.micRMS),
                    "play_rms": Double(decision.playRMS),
                    "corr": Double(decision.correlation),
                    "residual": Double(decision.residualRMS),
                    "onset_ms": decision.onsetMs,
                ])
                let confirmed = DispatchTime.now().uptimeNanoseconds
                let prerollData = preroll.snapshot(fromOnsetMs: max(decision.onsetMs, 80), padMs: 60)
                let candidateToConfirmed = decision.candidateNs == 0
                    ? 0
                    : Double(decision.confirmedNs.subtractingReportingOverflow(decision.candidateNs).partialValue) / 1_000_000.0
                let event = BargeInInterrupt(
                    preroll: prerollData,
                    audioPlayedMs: playback.playedMs,
                    confidence: decision.confidence,
                    prerollMs: prerollData.count * 1000 / max(1, 16_000 * 2),
                    candidateToConfirmedMs: candidateToConfirmed,
                    confirmedToForwardMs: Double(DispatchTime.now().uptimeNanoseconds.subtractingReportingOverflow(confirmed).partialValue) / 1_000_000.0
                )
                lastInterrupt = event
                interrupt(event)
                machine.completeInterrupt()
                preroll.reset()
                detector.reset()
            }
            return
        }
        if machine.canForwardMicToProvider(echoGate: playback.echoGate) {
            forward(pcm)
        }
    }
}
