import AppKit
import EVClient
import Foundation

/// EvieListenerPresenceEngine host for the Mac app.
///
/// Owns the four responsibilities with hard lane separation:
/// 1. ``BackchannelOpportunityDetector`` (timing, acoustic, no timers)
/// 2. ``BackchannelPolicy`` (should/what, stochastic, history-aware)
/// 3. Auxiliary playback through ``TTSPlayer.enqueueListenerFeedback``
///    (soft cached Evie variants; never an assistant turn)
/// 4. ``ListenerTelemetry`` (codes only, never transcripts)
///
/// THREADING LAW: the microphone tap thread only ever calls
/// ``ingestAcoustic(rms:frameMs:)`` — pure arithmetic feeding the detector.
/// Every policy decision and ALL playback scheduling hop to the main actor.
/// No audio-graph lifecycle mutation can ever run on a realtime callback.
@MainActor
final class ListenerPresenceController {
    struct Flags: Equatable {
        var enabled = UserDefaults.standard.bool(forKey: "EV_LISTENER_PRESENCE_ENABLED")
        var vocalEnabled = UserDefaults.standard.bool(forKey: "EV_LISTENER_VOCAL_ENABLED")
        var visualEnabled = UserDefaults.standard.bool(forKey: "EV_LISTENER_VISUAL_ENABLED")
        /// Lexical/contextual gestures ("nice", "okay", "right", "I see").
        /// OFF by default: recovery canary runs nonlexical-only until audio
        /// ownership and floor behavior are owner-verified.
        var semanticEnabled = UserDefaults.standard.bool(forKey: "EV_LISTENER_SEMANTIC_ENABLED")

        static func refresh() -> Flags { Flags() }
    }

    private(set) var flags = Flags.refresh()
    private let detector = BackchannelOpportunityDetector()
    private let policy = BackchannelPolicy()
    private var variants: [ListenerVariant] = []
    private var frameIndex: UInt64 = 0
    private var clockStart = Date()
    private var partialTail: String?
    private var partialLastChangedAt: Date?
    private var assistantResponsePending = false
    private var toolConfirmationActive = false
    private var audioRouteUnsafe = false
    private var lastPartialBroadcastAt = Date.distantPast
    /// FLOOR EPOCH — increments every time the conversational floor leaves the
    /// owner (assistant response starts) or an owner turn definitively ends.
    /// A candidate selected under one epoch can NEVER render under another.
    private(set) var floorEpoch: UInt64 = 0
    /// Post-response speaker/room tail guard: after Evie's answer finishes,
    /// residual acoustic energy must not seed a fake owner turn. Bounded from
    /// the existing captureEchoTail evidence (140 ms) widened for room decay.
    private var echoTailUntil = Date.distantPast
    /// Metrics (directive P0 round three). renderedDuringAssistant is an
    /// INVARIANT counter — it must remain zero forever.
    private(set) var metrics = Metrics()

    struct Metrics {
        var candidatesTotal = 0
        var vocalSelectedTotal = 0
        var renderedTotal = 0
        var suppressedAssistantFloor = 0
        var droppedStaleFloorEpoch = 0
        var renderedDuringAssistant = 0
    }
    /// Diagnostic provider installed by LiveConversation: barge-in detector +
    /// turn-machine state at the moment a nod is selected (LP05). Optional so
    /// the controller stays testable without a live session.
    private var detectorStateProvider: (() -> [String: Any])?
    /// Rate-limit bookkeeping for the owner-continues diagnostics (LP04/LP09).
    private var lastVocalEnqueueAt = Date.distantPast
    private var lastOwnerContinuesLogAt = Date.distantPast

    /// Main-actor only. Called by LiveConversation.attach.
    func installDetectorStateProvider(_ provider: @escaping () -> [String: Any]) {
        detectorStateProvider = provider
    }

    init() {
        loadVariants()
        enabledBox.lock.lock()
        enabledBox.value = flags.enabled
        enabledBox.lock.unlock()
        ListenerTelemetry.log("runtime", [
            "enabled": flags.enabled,
            "vocal": flags.vocalEnabled,
            "visual": flags.visualEnabled,
            "variants": variants.count,
        ])
    }

    func flagsDidUpdate() {
        flags = .refresh()
        enabledBox.lock.lock()
        enabledBox.value = flags.enabled
        enabledBox.lock.unlock()
    }

    private func nowMs() -> Int {
        Int(Date().timeIntervalSince(clockStart) * 1000)
    }

    // MARK: Variant cache (pre-rendered soft Evie micro-utterances)

    private func cacheDirectory() -> URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("EV/listener", isDirectory: true)
    }

    /// manifest.json: [{ "id": "mhm-soft-1", "class": "neutralContinuer", "file": "mhm-soft-1.pcm16" }]
    private func loadVariants() {
        let dir = cacheDirectory()
        let manifestURL = dir.appendingPathComponent("manifest.json")
        guard let data = try? Data(contentsOf: manifestURL),
              let entries = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
        else {
            ListenerTelemetry.log("variants.missing", ["dir": dir.path])
            return
        }
        var loaded: [ListenerVariant] = []
        for entry in entries {
            guard let id = entry["id"] as? String,
                  let file = entry["file"] as? String,
                  let kindRaw = entry["class"] as? String,
                  let kind = ListenerFeedbackClass(rawValue: kindRaw),
                  let pcm = try? Data(contentsOf: dir.appendingPathComponent(file)),
                  pcm.count > 2_000 // reject stubs shorter than ~60 ms
            else { continue }
            let family = ListenerDurationFamily(rawValue: entry["family"] as? String ?? "") ?? .normal
            let gain = (entry["gain"] as? NSNumber)?.floatValue ?? 0.34
            loaded.append(
                ListenerVariant(id: id, kind: kind, pcm16: pcm, family: family, gain: gain)
            )
        }
        variants = loaded
    }

    // MARK: Turn lifecycle (fed by LiveConversation)

    func noteOwnerTurnStarted() {
        detector.resetTurn()
        policy.resetTurn()
    }

    func noteOwnerTurnEnded() {
        noteOwnerTurnStarted()
        partialTail = nil
        partialLastChangedAt = nil
    }

    /// Stable-partial lexical context. Timing NEVER waits on this.
    func notePartial(_ text: String?) {
        guard flags.enabled else { return }
        let tail = text.map { $0.suffix(240) }.map(String.init)
        if tail != partialTail {
            partialTail = tail
            partialLastChangedAt = Date()
        }
        broadcastContextIfNeeded()
    }

    func noteAssistantResponse(active: Bool) {
        guard assistantResponsePending != active else { return }
        assistantResponsePending = active
        if active {
            // The floor left the owner. Every pending/future candidate from
            // the previous epoch is dead, and opportunity generation goes
            // DORMANT (hard, not probabilistic) for the whole response.
            floorEpoch &+= 1
            detector.resetTurn()
            policy.resetTurn()
            ListenerTelemetry.log("floor.assistant_has_floor", ["epoch": Int(floorEpoch)])
        } else {
            // Speaker + room tail: Evie's audio does not vanish at the last
            // sample. Bounded guard before the engine may listen again; a
            // fresh owner onset is then required (detector episode restarts).
            echoTailUntil = Date().addingTimeInterval(Self.postPlaybackEchoTailSeconds)
            detector.resetTurn()
        }
    }

    /// Dormancy is a PURE predicate so the hard gate is deterministically
    /// testable without audio hardware. Lives in EVClient as
    /// ``ListenerFloorGate`` — one authority, shared with tests.
    nonisolated static func isDormant(
        assistantResponsePending: Bool,
        responseLanePlaying: Bool,
        echoTailActive: Bool
    ) -> Bool {
        ListenerFloorGate.isDormant(
            assistantResponsePending: assistantResponsePending,
            responseLanePlaying: responseLanePlaying,
            echoTailActive: echoTailActive
        )
    }

    func noteToolHold(active: Bool) {
        toolConfirmationActive = active
    }

    func noteAudioRoute(unsafe: Bool) {
        audioRouteUnsafe = unsafe
    }

    private func broadcastContextIfNeeded() {
        let now = Date()
        guard now.timeIntervalSince(lastPartialBroadcastAt) >= 0.5 else { return }
        lastPartialBroadcastAt = now
        ListenerTelemetry.log("context.partial", [
            "tail_chars": partialTail?.count ?? 0,
        ])
    }

    // MARK: Acoustic feed (called from the realtime mic tap)

    /// Lock-guarded mirror of ``Flags.enabled`` readable from any thread.
    private final class EnabledBox: @unchecked Sendable {
        let lock = NSLock()
        var value = false
    }

    private let enabledBox = EnabledBox()

    /// O(frame) arithmetic only. Runs on the AVAudioEngine tap thread beside
    /// the barge-in detector; allocates nothing, touches no graph state, and
    /// hops every opportunity to the main actor for decision + rendering.
    nonisolated func ingestAcoustic(_ pcm16: Data) {
        enabledBox.lock.lock()
        let enabled = enabledBox.value
        enabledBox.lock.unlock()
        guard enabled else { return }
        let n = pcm16.count / 2
        guard n > 0 else { return }
        let rms = Self.rms16(pcm16)
        let frameMs = n * 1_000 / 16_000
        Task { @MainActor [weak self] in
            self?.ingestAcousticMain(rms: rms, frameMs: frameMs)
        }
    }

    private func ingestAcousticMain(rms: Float, frameMs: Int) {
        frameIndex &+= 1
        noteOwnerSpeechDuringFeedback(rms: rms)
        // HARD DORMANCY: while Evie owns the floor (or her speaker tail is
        // still decaying) the opportunity engine does not run AT ALL. Raw mic
        // energy during that window is SELF AUDIO, never owner speech.
        let responseLanePlaying = model?.player.isPlaying ?? false
        let echoTailActive = Date() < echoTailUntil
        if Self.isDormant(
            assistantResponsePending: assistantResponsePending,
            responseLanePlaying: responseLanePlaying,
            echoTailActive: echoTailActive
        ) {
            return
        }
        guard let opp = detector.ingest(
            OwnerFloorFeatures(frameIndex: frameIndex, rms: rms, frameMs: frameMs)
        ) else { return }
        handleOpportunity(opp, responseLanePlaying: responseLanePlaying, echoTailActive: echoTailActive)
    }

    /// LP04/LP09 — while a nod is on the auxiliary lane and the owner keeps
    /// talking, prove from traces that (a) the nod is NOT cancelled by that
    /// speech and (b) owner PCM capture stays open. Rate-limited to one pair
    /// of lines per 250 ms; pure logging, never touches playback.
    private func noteOwnerSpeechDuringFeedback(rms: Float) {
        let queued = model?.player.listenerFeedbackQueuedFrames ?? 0
        guard queued > 0 else { return }
        guard Date().timeIntervalSince(lastVocalEnqueueAt) < 2.5 else { return }
        guard Date().timeIntervalSince(lastOwnerContinuesLogAt) >= 0.25 else { return }
        lastOwnerContinuesLogAt = Date()
        let speaking = rms >= 0.010
        ListenerTelemetry.log("lp04.owner_speech_continues", [
            "owner_rms": Double(rms),
            "owner_vad_speaking": speaking,
            "aux_queued_frames": queued,
            "capture": "open",
        ])
        if speaking {
            ListenerTelemetry.log("lp09.owner_capture_continues", [
                "aux_queued_frames": queued,
                "barge_in_fired": false,
                "response_cancelled": false,
            ])
        }
    }

    private func handleOpportunity(
        _ opp: BackchannelOpportunity,
        responseLanePlaying: Bool,
        echoTailActive: Bool
    ) {
        guard flags.enabled else { return }
        // DEFENSE IN DEPTH: even if the low-level detector somehow produced a
        // candidate, the floor gate suppresses it. ASSISTANT_HAS_FLOOR is law.
        if Self.isDormant(
            assistantResponsePending: assistantResponsePending,
            responseLanePlaying: responseLanePlaying,
            echoTailActive: echoTailActive
        ) {
            metrics.suppressedAssistantFloor += 1
            ListenerTelemetry.log("suppressed.assistant_floor", [
                "reason": "ASSISTANT_HAS_FLOOR",
                "epoch": Int(floorEpoch),
                "response_pending": assistantResponsePending,
                "response_playing": responseLanePlaying,
                "echo_tail": echoTailActive,
            ])
            return
        }
        let epochAtSelection = floorEpoch
        metrics.candidatesTotal += 1
        let partialActive = Date().timeIntervalSince(partialLastChangedAt ?? .distantPast) < 1.2
        let context = ListenerTurnContext(
            partialTail: partialTail,
            partialActive: partialActive,
            assistantResponsePending: assistantResponsePending,
            toolConfirmationActive: toolConfirmationActive,
            audioRouteUnsafe: audioRouteUnsafe,
            semanticGesturesAllowed: flags.semanticEnabled
        )
        let decision = policy.decide(opp, context: context, available: variants, nowMs: nowMs())
        // LP00 — an acoustic opportunity survived the detector. Selection
        // outcome lands in the suppressed/visual/vocal logs below.
        ListenerTelemetry.log("lp00.opportunity_selected", [
            "speech_ms": opp.speechMs,
            "pause_ms": opp.pauseMs,
            "turn_end": Double(opp.turnEndProbability),
            "decay": Double(opp.entryDecayRatio),
            "adapted_pause_ms": opp.adaptedPauseMs,
            "epoch": Int(epochAtSelection),
        ])
        apply(decision, opportunity: opp, epochAtSelection: epochAtSelection)
    }

    private func apply(
        _ decision: ListenerDecision,
        opportunity: BackchannelOpportunity,
        epochAtSelection: UInt64
    ) {
        switch decision {
        case .nothing(let reason):
            ListenerTelemetry.log("suppressed", [
                "reason": reason.rawValue,
                "speech_ms": opportunity.speechMs,
                "pause_ms": opportunity.pauseMs,
                "turn_end": Double(opportunity.turnEndProbability),
                "decay": Double(opportunity.entryDecayRatio),
            ])
        case .visualOnly:
            policy.noteVisualEmitted()
            if flags.visualEnabled {
                VoiceLevelMeter.shared.noteListenerPulse()
            }
            ListenerTelemetry.log("visual", [
                "speech_ms": opportunity.speechMs,
                "pause_ms": opportunity.pauseMs,
            ])
        case .vocal(let kind, let variantId):
            // STALE-CANDIDATE LAW: the floor changed between selection and
            // render — this candidate belongs to a dead owner turn. Drop it.
            guard floorEpoch == epochAtSelection else {
                metrics.droppedStaleFloorEpoch += 1
                ListenerTelemetry.log("dropped.stale_floor_epoch", [
                    "variant": variantId,
                    "selected_epoch": Int(epochAtSelection),
                    "current_epoch": Int(floorEpoch),
                ])
                return
            }
            guard flags.vocalEnabled,
                  let variant = variants.first(where: { $0.id == variantId }),
                  let player = model?.player
            else {
                VoiceLevelMeter.shared.noteListenerPulse()
                ListenerTelemetry.log("vocal.unavailable", [
                    "variant": variantId,
                    "kind": kind.rawValue,
                ])
                return
            }
            // LP01 — variant chosen; LP05 — barge-in detector state AT
            // selection, so any later stop can be attributed against the
            // armed-against-response-only invariant.
            ListenerTelemetry.log("lp01.variant_selected", [
                "variant": variantId,
                "kind": kind.rawValue,
                "family": variant.family.rawValue,
                "gain": Double(variant.gain),
                "duration_ms": variant.pcm16.count * 1000 / max(1, variant.sampleRate * 2),
            ])
            var detectorState: [String: Any] = ["provider": "absent"]
            if let provider = detectorStateProvider {
                detectorState = provider()
            }
            ListenerTelemetry.log("lp05.barge_in_detector_state", detectorState)
            do {
                try player.enqueueListenerFeedback(
                    variant.pcm16,
                    sampleRate: Double(variant.sampleRate),
                    gain: variant.gain,
                    role: .listenerBackchannel,
                    completionPolicy: .finishDespiteOwnerSpeech
                )
                lastVocalEnqueueAt = Date()
                metrics.vocalSelectedTotal += 1
                metrics.renderedTotal += 1
                if assistantResponsePending || (model?.player.isPlaying ?? false) {
                    // INVARIANT counter — must remain zero forever.
                    metrics.renderedDuringAssistant += 1
                    ListenerTelemetry.log("invariant.violation.rendered_during_assistant", [
                        "variant": variantId,
                        "epoch": Int(floorEpoch),
                    ])
                }
                ListenerTelemetry.log("vocal", [
                    "variant": variantId,
                    "kind": kind.rawValue,
                    "family": variant.family.rawValue,
                    "speech_ms": opportunity.speechMs,
                    "pause_ms": opportunity.pauseMs,
                    "turn_end": Double(opportunity.turnEndProbability),
                    "queued_frames": player.listenerFeedbackQueuedFrames,
                    "epoch": Int(epochAtSelection),
                ])
            } catch {
                // Expendable signal: a failed nod must never touch the live session.
                VoiceLevelMeter.shared.noteListenerPulse()
                ListenerTelemetry.log("vocal.failed", ["error": error.localizedDescription])
            }
        }
    }

    /// Post-playback speaker/room tail seconds. Derived from the existing
    /// captureEchoTail evidence (140 ms transport tail) widened for room decay.
    nonisolated static let postPlaybackEchoTailSeconds: TimeInterval = 0.35

    private weak var model: AppModel?

    func attach(_ model: AppModel) {
        self.model = model
    }
}

private extension ListenerPresenceController {
    /// Allocation-free RMS over int16 little-endian PCM.
    nonisolated static func rms16(_ pcm: Data) -> Float {
        let count = pcm.count / 2
        guard count > 0 else { return 0 }
        var sum: Float = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: UInt8.self)
            var i = 0
            while i + 1 < samples.count {
                let value = Float(Int16(bitPattern: UInt16(samples[i]) | UInt16(samples[i &+ 1]) << 8))
                sum += value * value
                i += 2
            }
        }
        return sqrt(sum / Float(count)) / 32_768
    }
}
