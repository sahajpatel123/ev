import AVFoundation
import EVClient
import Foundation

/// Plays spoken audio. Live Grok Voice sends a stream of 16 kHz PCM frames;
/// those are scheduled on `AVAudioPlayerNode` so words do not restart a new
/// `AVAudioPlayer` (that gap is what made her sound laggy). Container files
/// (WAV / MP3 from push-to-talk) still use `AVAudioPlayer`.
final class TTSPlayer: NSObject, AVAudioPlayerDelegate {
    var onPlayingChange: ((Bool) -> Void)?

    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    /// ROLE B — listener-backchannel node. Physically separate from the
    /// response node so NO barge-in/response stop can ever decapitate a nod.
    /// This is BACKCHANNEL_COMPLETION_IMMUNITY by construction, not by flag.
    private let auxPlayerNode = AVAudioPlayerNode()
    private weak var sharedEngine: AVAudioEngine?
    private weak var attachedEngine: AVAudioEngine?
    private var usingSharedEngine = false
    private var engineReady = false
    private var engineSampleRate: Double?
    private var pendingBuffers = 0
    private var pendingFrames = 0
    private var streamGeneration = 0
    private var filePlayer: AVAudioPlayer?
    private var fileQueue: [Data] = []
    private let lock = NSLock()
    private var observers: [NSObjectProtocol] = []
    private var streamStartTask: DispatchWorkItem?
    private var drainWatchdog: DispatchWorkItem?
    private var playbackConverter: AVAudioConverter?
    private var converterSourceRate: Double?
    private var captureMuteUntil = Date.distantPast
    private var auxPendingFrames = 0
    /// Assistant EPISODE tracking: provider streaming gaps drain buffers and
    /// the reference ring while Evie is still mid-answer. The episode stays
    /// active for this long after the last response-lane chunk so turn
    /// decisions never flip on a pacing pause. (P0 round four: the 42 s chop
    /// happened inside exactly such a drain.)
    private var lastAssistantChunkAt = Date.distantPast
    private let assistantEpisodeGapTolerance: TimeInterval = 2.5
    /// ROLE B has its own generation: response-lane stop/regeneration cycles
    /// must NOT orphan in-flight nod bookkeeping.
    private var auxGeneration = 0
    private var auxSeq = 0
    // Completion-rate accounting (directive: selected/started/completed/
    // preempted/unexpected). Owner-continuation must never land in
    // "unexpected" — owner speech cannot reach any aux stop path at all.
    private var auxStartedFrames = 0
    private var auxCompletedFrames = 0
    private var auxPreemptedFrames = 0
    private var auxDroppedFrames = 0

    // Hold ~180 ms of converted audio before the first play() so normal
    // Realtime jitter (40-120 ms delta cadence) does not become an audible
    // underrun. Cap the wait to keep first-word latency low.
    private let streamPrimeDelay: TimeInterval = 0.02
    private let minStartSeconds: TimeInterval = 0.18
    private let primeRetryDelay: TimeInterval = 0.01
    private let maxPrimeWait: TimeInterval = 0.22
    private var streamPrimeDeadline = Date.distantPast
    // Minimal playback observability (no PCM): underrun + queue depth.
    private var underrunCount = 0
    private var scheduledBufferCount = 0
    private var lastEnqueueSampleRate: Double = 16_000
    // Connect the player at 48 kHz, the usual Mac HAL rate. Scheduling 16 kHz
    // buffers on that graph (or returning the 16 kHz source when conversion
    // fails) plays the reply at the wrong speed. Convert with one streaming
    // converter and never schedule a mismatched format.
    private let playbackSampleRate: Double = 48_000
    private let captureEchoTail: TimeInterval = 0.14
    private var playbackFalseTask: DispatchWorkItem?
    private var playedFrames: Int = 0
    private var playbackBeganAt: Date?
    private var referencePCM = Data()
    /// Far-end reference ring for interruption ownership (V3 final): holds
    /// 4 s of recently ENQUEUED response PCM so delay-aware echo matching has
    /// a real alignment window. The old 160 ms ring made ownership
    /// correlation structurally return zero (proven in the V3 diagnostic
    /// session: corr=0.0 on every partial while Evie's voice dominated mic).
    private let referenceKeepBytes = 16_000 * 2 * 4000 / 1000

    override init() {
        super.init()
        observers.append(
            NotificationCenter.default.addObserver(
                forName: .AVAudioEngineConfigurationChange,
                object: nil,
                queue: nil
            ) { [weak self] notification in
                guard let self else { return }
                var shouldReset = false
                self.lock.lock()
                if let engine = self.attachedEngine,
                   !self.usingSharedEngine,
                   let changed = notification.object as? AVAudioEngine,
                   changed === engine {
                    self.engineReady = false
                    shouldReset = true
                }
                self.lock.unlock()
                if shouldReset {
                    self.stopStream(notify: true, reason: "engine_configuration_change")
                }
            }
        )
    }

    var isPlaying: Bool {
        lock.lock()
        let queued = pendingBuffers > 0
        let file = filePlayer?.isPlaying ?? false
        lock.unlock()
        return queued || file
    }

    /// True while speakers are live or residual room echo could still barge in.
    /// Safe to call from the microphone tap thread.
    ///
    /// Do not use `AVAudioPlayerNode.isPlaying`. The node stays "playing"
    /// after the last buffer finishes until someone pauses it, which used to
    /// mute the mic for seconds after she stopped talking.
    var shouldMuteCapture: Bool {
        lock.lock()
        let queued = pendingBuffers > 0
        let file = filePlayer?.isPlaying ?? false
        let echo = Date() < captureMuteUntil
        lock.unlock()
        return queued || file || echo
    }

    /// What the speakers are rendering, for the local barge-in detector.
    /// Safe to call from the microphone tap thread.
    func playbackSnapshot() -> PlaybackSnapshot {
        lock.lock()
        let pcm = referencePCM
        let queued = pendingBuffers > 0
        let file = filePlayer?.isPlaying ?? false
        let echo = Date() < captureMuteUntil
        let played = playedFrames
        let pending = pendingFrames
        let began = playbackBeganAt
        let lastChunkAge = Date().timeIntervalSince(lastAssistantChunkAt)
        lock.unlock()
        let rms = Self.pcm16RMS(pcm)
        let audible = queued || file
        let elapsedMs = began.map { Int(Date().timeIntervalSince($0) * 1000) } ?? 0
        let completedMs = Int((Double(played) / max(playbackSampleRate, 1)) * 1000)
        let playedMs = max(elapsedMs, completedMs)
        let queuedMs = Int((Double(pending) / max(playbackSampleRate, 1)) * 1000)
        return PlaybackSnapshot(
            pcm16: pcm,
            rms: rms,
            audible: audible,
            echoGate: queued || file || echo,
            playedMs: playedMs,
            queuedMs: queuedMs,
            assistantEpisodeActive: audible || lastChunkAge < assistantEpisodeGapTolerance
        )
    }

    /// Attach the player node to the live capture engine *before* that engine
    /// starts. Mutating a running graph throws -10867.
    func bind(to engine: AVAudioEngine?) {
        guard let engine else {
            detachPlayerNode()
            return
        }
        if attachedEngine === engine, usingSharedEngine { return }
        stopStream(notify: false)
        detachPlayerNode()
        sharedEngine = engine
        attach(to: engine)
    }

    private func attach(to engine: AVAudioEngine) {
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: playbackSampleRate,
            channels: 1,
            interleaved: false
        )
        guard let format else { return }
        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: format)
        engine.attach(auxPlayerNode)
        engine.connect(auxPlayerNode, to: engine.mainMixerNode, format: format)
        engine.mainMixerNode.outputVolume = 1.0
        playerNode.volume = 1.0
        attachedEngine = engine
        usingSharedEngine = true
        engineReady = true
        engineSampleRate = playbackSampleRate
        resetPlaybackConverter()
    }

    func play(data: Data) throws {
        stop()
        try enqueue(data)
    }

    func enqueue(_ data: Data, contentType: String? = nil, sampleRate: Int = 16_000) throws {
        if shouldStreamPCM(data, contentType: contentType) {
            let pcm: Data
            let rate: Double
            if let wav = wavPCM(data) {
                pcm = wav.samples
                rate = wav.sampleRate
            } else {
                pcm = data
                rate = Double(sampleRate)
            }
            try enqueuePCM(pcm, sampleRate: rate)
            return
        }
        stopStream(notify: false, reason: "enqueue(container)")
        if filePlayer?.isPlaying == true {
            fileQueue.append(data)
            return
        }
        try playFile(data)
    }

    func stop() {
        stop(echoTail: true, auxTeardown: true, reason: "stop()")
    }

    /// ROLE C stop: barge-in against a NORMAL assistant response.
    /// By construction this can NEVER chop a listener nod — nods live on
    /// ``auxPlayerNode`` and this function does not touch it. Owner speech
    /// during a nod is expected overlap, not an interruption.
    ///
    /// THREADING INVARIANT: never call this from an AVAudioEngine tap or
    /// render callback. `AVAudioPlayerNode.stop()` dispatch-syncs the
    /// engine's RealtimeMessenger queue; from inside that queue it is a
    /// guaranteed deadlock and macOS kills the process (EXC_BREAKPOINT,
    /// EV-2026-08-21-*.ips). Hop to another queue first — see
    /// ``LiveConversation.bargeInControlQueue``.
    /// ROLE C ONLY. `auxTeardown: false` is the code-level statement of
    /// BACKCHANNEL_COMPLETION_IMMUNITY: a barge-in against a normal response
    /// physically cannot cancel a listener nod.
    func stopForBargeIn() {
        stop(echoTail: false, auxTeardown: false, reason: "stopForBargeIn(roleC)")
    }

    private func stop(echoTail: Bool, auxTeardown: Bool, reason: String) {
        let wasPlaying = isPlaying
        fileQueue.removeAll()
        filePlayer?.delegate = nil
        filePlayer?.stop()
        filePlayer = nil
        stopStream(notify: false, echoTail: echoTail, auxTeardown: auxTeardown, reason: reason)
        if wasPlaying {
            onPlayingChange?(false)
        }
    }

    /// Reset a failed private playback graph without claiming that audio was
    /// delivered. The next chunk rebuilds/restarts the graph.
    func recover() {
        let had = isPlaying
        fileQueue.removeAll()
        filePlayer?.delegate = nil
        filePlayer?.stop()
        filePlayer = nil
        stopStream(notify: had, auxTeardown: true, reason: "recover()")
        if !usingSharedEngine {
            engine.stop()
            engineReady = false
        }
    }

    /// Called when a brand-new user turn begins so stale streamed PCM or
    /// container audio left over from the previous response is dropped instead
    /// of continuing to play underneath the new reply.
    func prepareForNewTurn() {
        lock.lock()
        let hadAudio = pendingBuffers > 0
            || filePlayer?.isPlaying == true
            || !fileQueue.isEmpty
        lock.unlock()
        guard hadAudio else { return }
        stop(echoTail: true, auxTeardown: true, reason: "prepareForNewTurn()")
    }

    /// Provider finished this spoken reply. Start any primed audio that is
    /// still sitting in the queue, then reopen the mic if completions never
    /// fire after the queued duration has elapsed.
    func noteAssistantAudioComplete() {
        lock.lock()
        let generation = streamGeneration
        let pending = pendingBuffers
        let queuedSeconds = Double(pendingFrames) / max(playbackSampleRate, 1)
        lock.unlock()
        if pending > 0, !playerNode.isPlaying {
            streamStartTask?.cancel()
            streamStartTask = nil
            playerNode.play()
        }
        guard pending > 0 else { return }
        armDrainWatchdog(generation: generation, after: min(max(queuedSeconds + 0.35, 0.40), 8.0))
    }

    private func shouldStreamPCM(_ data: Data, contentType: String?) -> Bool {
        let kind = (contentType ?? "").lowercased()
        if kind.contains("pcm") || kind.contains("l16") {
            return true
        }
        if kind.contains("mpeg") || kind.contains("mp3") || kind.contains("mp4") || kind.contains("aac") {
            return false
        }
        if data.starts(with: Data("ID3".utf8)) {
            return false
        }
        if data.count >= 2, data[0] == 0xFF, data[1] & 0xE0 == 0xE0 {
            return false
        }
        if kind.contains("wav") || data.starts(with: Data("RIFF".utf8)) {
            return true
        }
        if kind.contains("pcm") || kind.contains("l16") || kind.isEmpty {
            return true
        }
        return false
    }

    private func ensureEngine(sampleRate: Double) throws {
        let graphRate = playbackSampleRate
        if usingSharedEngine, let shared = sharedEngine {
            if !shared.isRunning {
                shared.isAutoShutdownEnabled = false
                shared.prepare()
                try shared.start()
            }
            return
        }
        if engineReady, attachedEngine === engine, engineSampleRate == graphRate {
            if !engine.isRunning {
                engine.isAutoShutdownEnabled = false
                engine.prepare()
                try engine.start()
            }
            return
        }
        if attachedEngine != nil {
            detachPlayerNode()
        }
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: graphRate,
            channels: 1,
            interleaved: false
        )
        guard let format else {
            throw NSError(domain: "EVTTS", code: 1, userInfo: [NSLocalizedDescriptionKey: "unable to create PCM format"])
        }
        engine.isAutoShutdownEnabled = false
        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: format)
        engine.attach(auxPlayerNode)
        engine.connect(auxPlayerNode, to: engine.mainMixerNode, format: format)
        engine.mainMixerNode.outputVolume = 1.0
        playerNode.volume = 1.0
        engine.prepare()
        try engine.start()
        attachedEngine = engine
        engineSampleRate = graphRate
        engineReady = true
        resetPlaybackConverter()
        _ = sampleRate
    }

    private func detachSharedPlayer() {
        guard usingSharedEngine else { return }
        streamStartTask?.cancel()
        streamStartTask = nil
        drainWatchdog?.cancel()
        drainWatchdog = nil
        streamPrimeDeadline = .distantPast
        playerNode.stop()
        playerNode.reset()
        var droppedFrames = 0
        lock.lock()
        streamGeneration += 1
        scheduledBufferCount = 0
        underrunCount = 0
        pendingBuffers = 0
        pendingFrames = 0
        droppedFrames = auxPendingFrames
        if droppedFrames > 0 {
            auxDroppedFrames += droppedFrames
            auxGeneration += 1
        }
        auxPendingFrames = 0
        playedFrames = 0
        playbackBeganAt = nil
        referencePCM.removeAll(keepingCapacity: true)
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        lock.unlock()
        if droppedFrames > 0 {
            ListenerTelemetry.log("acct", ["where": "detachdrop", "delta": droppedFrames])
            ListenerTelemetry.log("lp06.aux_teardown", ["who": "detachSharedPlayer", "why": "graph_recovery", "frames": droppedFrames])
        }
        ListenerTelemetry.log("lp06.aux_teardown", ["who": "detachSharedPlayer", "why": "graph_recovery", "frames": 0])
        if let shared = sharedEngine {
            shared.disconnectNodeOutput(playerNode)
            shared.detach(playerNode)
        }
        usingSharedEngine = false
        engineReady = false
        attachedEngine = nil
        engineSampleRate = nil
        resetPlaybackConverter()
    }

    private func detachPlayerNode() {
        streamStartTask?.cancel()
        streamStartTask = nil
        drainWatchdog?.cancel()
        drainWatchdog = nil
        streamPrimeDeadline = .distantPast
        playerNode.stop()
        playerNode.reset()
        var droppedFrames = 0
        lock.lock()
        streamGeneration += 1
        scheduledBufferCount = 0
        underrunCount = 0
        pendingBuffers = 0
        pendingFrames = 0
        droppedFrames = auxPendingFrames
        if droppedFrames > 0 {
            auxDroppedFrames += droppedFrames
            auxGeneration += 1
        }
        auxPendingFrames = 0
        playedFrames = 0
        playbackBeganAt = nil
        referencePCM.removeAll(keepingCapacity: true)
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        lock.unlock()
        if droppedFrames > 0 {
            ListenerTelemetry.log("acct", ["where": "detachdrop", "delta": droppedFrames])
            ListenerTelemetry.log("lp06.aux_teardown", ["who": "detachPlayerNode", "why": "rebind_or_teardown", "frames": droppedFrames])
        }
        ListenerTelemetry.log("lp06.aux_teardown", ["who": "detachPlayerNode", "why": "rebind_or_teardown", "frames": 0])
        if let attached = attachedEngine {
            if !usingSharedEngine, attached.isRunning {
                attached.stop()
            }
            attached.disconnectNodeOutput(playerNode)
            attached.detach(playerNode)
        }
        usingSharedEngine = false
        engineReady = false
        attachedEngine = nil
        sharedEngine = nil
        engineSampleRate = nil
        resetPlaybackConverter()
    }

    private func enqueuePCM(_ pcm: Data, sampleRate: Double) throws {
        guard !pcm.isEmpty else { return }
        lock.lock()
        lastAssistantChunkAt = Date()
        lock.unlock()
        rememberPlaybackReference(pcm, sampleRate: sampleRate)
        VoiceLevelMeter.shared.ingestOutputPCM16(pcm)
        do {
            try schedulePCM(pcm, sampleRate: sampleRate)
        } catch {
            guard usingSharedEngine else { throw error }
            detachSharedPlayer()
            try schedulePCM(pcm, sampleRate: sampleRate)
        }
    }

    // MARK: Listener presence lane (ROLE B — auxiliary, NOT an assistant turn)

    /// Explicit playback semantics. Role is DECLARED, never inferred from
    /// volume/duration/player state.
    enum PlaybackRole {
        /// ROLE C: Evie holds the floor; owner speech may legitimately stop it.
        case normalAssistantResponse
        /// ROLE B: the OWNER holds the floor; overlap is expected by design.
        case listenerBackchannel
    }

    /// How an auxiliary sample may end. Only the listed causes exist.
    enum AuxCompletionPolicy {
        /// Owner speech during playback is expected overlap, never a stop.
        case finishDespiteOwnerSpeech
    }

    /// Frames of listener-feedback audio queued on the auxiliary lane.
    var listenerFeedbackQueuedFrames: Int {
        lock.lock()
        defer { lock.unlock() }
        return auxPendingFrames
    }

    /// Frame-based completion accounting. Invariant:
    /// started == completed + preempted + dropped (unexpected == 0).
    /// Owner continuation can never produce a "dropped" or "preempted" frame.
    var listenerFeedbackAccounting: (started: Int, completed: Int, preempted: Int, dropped: Int) {
        lock.lock()
        defer { lock.unlock() }
        return (auxStartedFrames, auxCompletedFrames, auxPreemptedFrames, auxDroppedFrames)
    }

    /// Soft listener-feedback playback ("mhmmm" nods) while the owner speaks.
    ///
    /// LANE CONTRACT — deliberately different from ``enqueuePCM``:
    /// - schedules on ``auxPlayerNode``, a physically separate node. The
    ///   response lane's stop/reset paths CANNOT touch it: owner continuation,
    ///   barge-in confirmation, and response cancellation are all structurally
    ///   unable to chop a nod (completion immunity);
    /// - never raises `onPlayingChange`, so `assistantSpeaking` stays false
    ///   while `listenerFeedbackQueuedFrames > 0` reports ROLE B activity;
    /// - never sets the capture echo-tail mute, so owner PCM keeps flowing to
    ///   the provider while the nod plays;
    /// - PCM IS remembered in the self-playback reference so the local
    ///   barge-in detector correlates it away as self audio;
    /// - ends ONLY via natural completion, ``preemptListenerFeedbackForResponse``,
    ///   or route/session teardown.
    ///
    /// THREADING: main thread only. Never call from an audio callback.
    func enqueueListenerFeedback(
        _ pcm16: Data,
        sampleRate: Double = 16_000,
        gain: Float = 0.34,
        role: PlaybackRole = .listenerBackchannel,
        completionPolicy: AuxCompletionPolicy = .finishDespiteOwnerSpeech
    ) throws {
        guard role == .listenerBackchannel else {
            throw NSError(domain: "EVTTS", code: 4, userInfo: [NSLocalizedDescriptionKey: "non-aux role rejected on listener lane"])
        }
        guard completionPolicy == .finishDespiteOwnerSpeech else { return }
        guard !pcm16.isEmpty else { return }
        let scaled = Self.scaledPCM16(pcm16, gain: gain)
        rememberPlaybackReference(scaled, sampleRate: sampleRate)
        do {
            try scheduleAuxiliary(scaled, sampleRate: sampleRate)
        } catch {
            guard usingSharedEngine else { throw error }
            detachSharedPlayer()
            try scheduleAuxiliary(scaled, sampleRate: sampleRate)
        }
    }

    private func scheduleAuxiliary(_ pcm: Data, sampleRate: Double) throws {
        try ensureEngine(sampleRate: sampleRate)
        guard !usingSharedEngine || sharedEngine?.isRunning != false else {
            throw NSError(domain: "EVTTS", code: 3, userInfo: [NSLocalizedDescriptionKey: "shared engine not running"])
        }
        guard let buffer = playbackBuffer(from: pcm, sourceRate: sampleRate) else { return }
        let frameCount = Int(buffer.frameLength)
        lock.lock()
        auxPendingFrames += frameCount
        auxStartedFrames += frameCount
        auxSeq += 1
        let seq = auxSeq
        let generation = auxGeneration
        lock.unlock()
        ListenerTelemetry.log("lp02.aux_enqueue", ["seq": seq, "frames": frameCount, "q": auxPendingFrames])
        auxPlayerNode.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            // Lock discipline: counters only under the lock; telemetry and
            // any I/O strictly outside it. This callback shares the lock
            // with teardown paths that stop nodes — holding it across
            // node/IO work here would deadlock the stop.
            self.lock.lock()
            let valid = self.auxGeneration == generation
            if valid {
                self.auxPendingFrames = max(0, self.auxPendingFrames - frameCount)
                self.auxCompletedFrames += frameCount
            }
            let qAfter = self.auxPendingFrames
            self.lock.unlock()
            ListenerTelemetry.log("acct", ["where": "complete", "seq": seq, "delta": valid ? frameCount : 0, "valid": valid, "q": qAfter])
            if valid {
                if qAfter == 0 {
                    ListenerTelemetry.log("lp07.aux_last_sample", ["seq": seq, "frames": frameCount])
                }
                ListenerTelemetry.log("lp08.aux_complete", ["frames": frameCount])
            }
        }
        if !auxPlayerNode.isPlaying {
            auxPlayerNode.play()
            ListenerTelemetry.log("lp03.aux_first_sample_armed", [:])
        }
    }

    /// THE ONLY sanctioned early cut of a listener nod: a NORMAL response is
    /// ready and needs the airwaves (NORMAL_RESPONSE > LISTENER_BACKCHANNEL).
    /// Safe control path (main thread) only — NEVER from a mic callback.
    /// Owner speech can never reach this function.
    func preemptListenerFeedbackForResponse(reason: String) {
        lock.lock()
        let droppedFrames = auxPendingFrames
        let had = droppedFrames > 0
        if had {
            auxPreemptedFrames += droppedFrames
            auxPendingFrames = 0
        }
        lock.unlock()
        guard had else { return }
        auxPlayerNode.stop()
        auxPlayerNode.reset()
        ListenerTelemetry.log("acct", ["where": "preempt", "delta": droppedFrames, "q": auxPendingFrames])
        ListenerTelemetry.log("lp06.aux_preempt", ["reason": reason])
    }

    /// Scale int16 little-endian PCM by a linear gain with clamping.
    private static func scaledPCM16(_ pcm: Data, gain: Float) -> Data {
        guard abs(gain - 1) > 0.001 else { return pcm }
        var out = pcm
        out.withUnsafeMutableBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<samples.count {
                let value = Float(Int16(littleEndian: samples[i])) * gain
                samples[i] = Int16(max(-32_767, min(32_767, value)).rounded())
            }
        }
        return out
    }

    private func schedulePCM(_ pcm: Data, sampleRate: Double) throws {
        try ensureEngine(sampleRate: sampleRate)
        // CV09/CV10 — client enqueue timing for continuity forensics
        // (env-gated; EV_AUDIO_CV_TRACE=1).
        if ProcessInfo.processInfo.environment["EV_AUDIO_CV_TRACE"] == "1" {
            let queuedMsNow = Int((Double(pendingFrames) / max(playbackSampleRate, 1)) * 1000)
            let playedMsNow = Int((Double(playedFrames) / max(playbackSampleRate, 1)) * 1000)
            BargeInTrace.log("cv.client.enqueue", [
                "mono_ns": DispatchTime.now().uptimeNanoseconds,
                "bytes": pcm.count,
                "queued_ms": queuedMsNow,
                "played_ms": playedMsNow,
            ])
        }
        guard let buffer = playbackBuffer(from: pcm, sourceRate: sampleRate) else { return }
        let frameCount = Int(buffer.frameLength)
        let wasPlaying = playerNode.isPlaying
        let prevPending: Int
        lock.lock()
        prevPending = pendingBuffers
        pendingBuffers += 1
        pendingFrames += frameCount
        scheduledBufferCount += 1
        lastEnqueueSampleRate = sampleRate
        if playbackBeganAt == nil {
            playbackBeganAt = Date()
        }
        let generation = streamGeneration
        lock.unlock()
        // Underrun: we were mid-response (had scheduled before) but queue had drained to 0 and node stopped.
        if !wasPlaying, prevPending == 0, scheduledBufferCount > 1 {
            lock.lock()
            underrunCount += 1
            lock.unlock()
            // Minimal playback observability: count only; detailed trace via
            // queue depth behavior already visible in pendingFrames/metrics.
        }
        notifyPlaying(true)
        playerNode.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.lock.lock()
            guard self.streamGeneration == generation else {
                self.lock.unlock()
                return
            }
            self.pendingBuffers = max(0, self.pendingBuffers - 1)
            self.pendingFrames = max(0, self.pendingFrames - frameCount)
            self.playedFrames += frameCount
            let idle = self.pendingBuffers == 0
            if idle {
                self.captureMuteUntil = Date().addingTimeInterval(self.captureEchoTail)
                self.playbackBeganAt = nil
                self.drainWatchdog?.cancel()
                self.drainWatchdog = nil
            }
            self.lock.unlock()
            if idle {
                self.notifyPlaying(false)
            }
        }
        if playerNode.isPlaying {
            lock.lock()
            let queuedSeconds = Double(pendingFrames) / max(playbackSampleRate, 1)
            lock.unlock()
            armDrainWatchdog(
                generation: generation,
                after: min(max(queuedSeconds + 0.45, 0.50), 8.0)
            )
            return
        }
        lock.lock()
        let queuedSeconds = Double(pendingFrames) / max(playbackSampleRate, 1)
        if streamPrimeDeadline == .distantPast {
            streamPrimeDeadline = Date().addingTimeInterval(maxPrimeWait)
        }
        lock.unlock()
        armDrainWatchdog(
            generation: generation,
            after: min(max(queuedSeconds + 0.45, 0.50), 8.0)
        )
        primeStreamPlayback(generation: generation)
    }

    private func notifyPlaying(_ playing: Bool) {
        if playing {
            playbackFalseTask?.cancel()
            playbackFalseTask = nil
            onPlayingChange?(true)
            return
        }
        playbackFalseTask?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let idle = self.pendingBuffers == 0
            self.lock.unlock()
            if idle {
                self.onPlayingChange?(false)
            }
        }
        playbackFalseTask = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18, execute: work)
    }

    private func primeStreamPlayback(generation: Int) {
        primeStreamPlayback(generation: generation, after: streamPrimeDelay)
    }

    private func primeStreamPlayback(generation scheduledGeneration: Int, after delay: TimeInterval) {
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard self.streamGeneration == scheduledGeneration else {
                self.lock.unlock()
                return
            }
            let hasAudio = self.pendingBuffers > 0
            let queuedSeconds = Double(self.pendingFrames) / self.playbackSampleRate
            let deadline = self.streamPrimeDeadline
            self.streamStartTask = nil
            self.lock.unlock()
            guard hasAudio, !self.playerNode.isPlaying else { return }
            if queuedSeconds < self.minStartSeconds, Date() < deadline {
                self.primeStreamPlayback(
                    generation: scheduledGeneration,
                    after: self.primeRetryDelay
                )
                return
            }
            self.playerNode.play()
        }
        streamStartTask = work
        DispatchQueue.main.asyncAfter(
            deadline: .now() + delay,
            execute: work
        )
    }

    private func armDrainWatchdog(generation: Int, after delay: TimeInterval) {
        drainWatchdog?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard self.streamGeneration == generation else {
                self.lock.unlock()
                return
            }
            let stuck = self.pendingBuffers > 0
            if stuck {
                self.pendingBuffers = 0
                self.pendingFrames = 0
                self.captureMuteUntil = Date().addingTimeInterval(self.captureEchoTail)
            }
            self.drainWatchdog = nil
            self.lock.unlock()
            if stuck {
                self.notifyPlaying(false)
            }
        }
        drainWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func stopStream(notify: Bool, echoTail: Bool = true, auxTeardown: Bool = true, reason: String = "unspecified") {
        playbackFalseTask?.cancel()
        playbackFalseTask = nil
        streamStartTask?.cancel()
        streamStartTask = nil
        drainWatchdog?.cancel()
        drainWatchdog = nil
        streamPrimeDeadline = .distantPast
        playerNode.stop()
        playerNode.reset()
        lock.lock()
        streamGeneration += 1
        scheduledBufferCount = 0
        underrunCount = 0
        let had = pendingBuffers > 0
        pendingBuffers = 0
        pendingFrames = 0
        var droppedNow = 0
        if auxTeardown {
            droppedNow = auxPendingFrames
            if droppedNow > 0 {
                auxDroppedFrames += droppedNow
                auxGeneration += 1
            }
            auxPendingFrames = 0
        }
        playbackBeganAt = nil
        if echoTail {
            captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        } else {
            captureMuteUntil = .distantPast
            playedFrames = 0
            playbackBeganAt = nil
            referencePCM.removeAll(keepingCapacity: true)
        }
        lock.unlock()
        // Node ops and telemetry live OUTSIDE the lock (completion callbacks
        // share this lock; holding it across node work deadlocks the stop).
        if droppedNow > 0 {
            auxPlayerNode.stop()
            auxPlayerNode.reset()
            ListenerTelemetry.log("acct", ["where": "teardown", "delta": droppedNow])
            ListenerTelemetry.log("lp06.aux_teardown", ["who": "stopStream", "why": reason, "frames": droppedNow])
        }
        resetPlaybackConverter()
        if notify, had {
            onPlayingChange?(false)
        }
    }

    private func rememberPlaybackReference(_ pcm: Data, sampleRate: Double) {
        let reference: Data
        if abs(sampleRate - 16_000) < 0.5 {
            reference = pcm
        } else {
            reference = pcm
        }
        lock.lock()
        referencePCM.append(reference)
        if referencePCM.count > referenceKeepBytes {
            referencePCM.removeFirst(referencePCM.count - referenceKeepBytes)
        }
        lock.unlock()
    }

    private static func pcm16RMS(_ pcm: Data) -> Float {
        let count = pcm.count / 2
        guard count > 0 else { return 0 }
        var sum: Float = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            let n = min(count, samples.count)
            for i in 0..<n {
                let value = Float(samples[i].littleEndian) / 32768.0
                sum += value * value
            }
        }
        return sqrt(sum / Float(count))
    }

    private func resetPlaybackConverter() {
        playbackConverter = nil
        converterSourceRate = nil
    }

    private func playbackBuffer(from pcm: Data, sourceRate: Double) -> AVAudioPCMBuffer? {
        guard let source = floatBuffer(from: pcm, sampleRate: sourceRate) else { return nil }
        if abs(sourceRate - playbackSampleRate) < 0.5 {
            return source
        }
        guard let destFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: playbackSampleRate,
            channels: 1,
            interleaved: false
        ) else {
            return nil
        }
        if playbackConverter == nil || converterSourceRate != sourceRate {
            playbackConverter = AVAudioConverter(from: source.format, to: destFormat)
            converterSourceRate = sourceRate
        }
        guard let converter = playbackConverter else { return nil }
        let ratio = playbackSampleRate / max(sourceRate, 1)
        let capacity = AVAudioFrameCount(Double(source.frameLength) * ratio) + 256
        guard let dest = AVAudioPCMBuffer(pcmFormat: destFormat, frameCapacity: capacity) else {
            return nil
        }
        var error: NSError?
        var provided = false
        let status = converter.convert(to: dest, error: &error) { _, outStatus in
            if provided {
                outStatus.pointee = .noDataNow
                return nil
            }
            provided = true
            outStatus.pointee = .haveData
            return source
        }
        guard status != .error, dest.frameLength > 0 else { return nil }
        return dest
    }

    deinit {
        streamStartTask?.cancel()
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    private func playFile(_ data: Data) throws {
        let wasPlaying = filePlayer?.isPlaying == true
        filePlayer?.delegate = nil
        filePlayer?.stop()
        let next = try AVAudioPlayer(data: data)
        next.delegate = self
        next.prepareToPlay()
        next.play()
        filePlayer = next
        if !wasPlaying {
            onPlayingChange?(true)
        }
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        if !fileQueue.isEmpty {
            let next = fileQueue.removeFirst()
            try? playFile(next)
            return
        }
        filePlayer = nil
        onPlayingChange?(false)
    }

    private func floatBuffer(from pcm: Data, sampleRate: Double) -> AVAudioPCMBuffer? {
        let n = pcm.count / 2
        guard n > 0 else { return nil }
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false
        ) else { return nil }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(n)) else {
            return nil
        }
        buffer.frameLength = AVAudioFrameCount(n)
        guard let dest = buffer.floatChannelData?[0] else { return nil }
        pcm.withUnsafeBytes { raw in
            let bytes = raw.bindMemory(to: UInt8.self)
            let count = min(n, bytes.count / 2)
            for i in 0..<count {
                let value = Int16(bitPattern: UInt16(bytes[i * 2]) | UInt16(bytes[i * 2 + 1]) << 8)
                dest[i] = Float(value) / 32768.0
            }
        }
        return buffer
    }
}

private struct WavPCM {
    let samples: Data
    let sampleRate: Double
}

private func wavPCM(_ data: Data) -> WavPCM? {
    guard data.count >= 44, data.starts(with: Data("RIFF".utf8)) else { return nil }
    var offset = 12
    var sampleRate = 16_000.0
    var bits: UInt16 = 16
    var channels: UInt16 = 1
    var payload: Data?
    while offset + 8 <= data.count {
        let id = String(data: data.subdata(in: offset..<(offset + 4)), encoding: .ascii) ?? ""
        let size = Int(u32LE(data, offset + 4))
        let start = offset + 8
        let end = min(data.count, start + size)
        if id == "fmt ", end - start >= 16 {
            channels = u16LE(data, start + 2)
            sampleRate = Double(u32LE(data, start + 4))
            bits = u16LE(data, start + 14)
        } else if id == "data" {
            payload = data.subdata(in: start..<end)
            break
        }
        offset = start + size + (size % 2)
    }
    guard let payload, bits == 16, channels == 1, sampleRate > 0 else { return nil }
    return WavPCM(samples: payload, sampleRate: sampleRate)
}

private func u16LE(_ data: Data, _ offset: Int) -> UInt16 {
    UInt16(data[offset]) | UInt16(data[offset + 1]) << 8
}

private func u32LE(_ data: Data, _ offset: Int) -> UInt32 {
    UInt32(data[offset])
        | UInt32(data[offset + 1]) << 8
        | UInt32(data[offset + 2]) << 16
        | UInt32(data[offset + 3]) << 24
}
