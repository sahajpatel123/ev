import AVFoundation
import EVClient
import Foundation

/// The sole EV.app speaker owner.
///
/// Realtime PCM is accepted on one serial queue, aggregated into 160 ms
/// buffers, converted once to the stable player format, and scheduled on one
/// AVAudioPlayerNode. The engine is never started from init and is stopped at
/// the end of the voice session.
final class TTSPlayer: NSObject, @unchecked Sendable {
    var onPlayingChange: ((Bool) -> Void)?

    private static let sourceChannels = 1
    private static let sourceBytesPerSample = 2
    private static let sourceBytesPerFrame = sourceChannels * sourceBytesPerSample
    private static let aggregationMs = 160
    private static let startupPrebufferMs = 280
    private static let targetLeadMs = 500
    private static let hardCeilingMs = 1500
    private static let echoTail: TimeInterval = 0.25

    private let audioQueue = DispatchQueue(label: "com.ev.audio.playback", qos: .userInitiated)
    private let queueKey = DispatchSpecificKey<UInt8>()
    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private let playerFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 48_000,
        channels: 1,
        interleaved: false
    )!

    private var engineConfigured = false
    private var sessionActive = false
    private var liveSessionOwned = false
    private var ephemeralSession = false
    private var streamGeneration = 0
    private var activeResponseID: String?
    private var responseFinished = false
    private var playerStarted = false
    private var sourceRate: Double?
    private var partialFrameBytes = Data()
    private var aggregatePCM = Data()
    private var converter: AVAudioConverter?
    private var converterSourceRate: Double?
    private var pendingBuffers = 0
    private var pendingFrames = 0
    private var reportedPlaying = false

    // Minimal per-response counters (directive §9). All touched on the serial
    // audio queue only. Frame counts are source-rate frames; lead and age are
    // duration-based so 16k→48k conversion ratios never distort them.
    private var receivedPCMBytes = 0
    private var pcmReceivedFrames = 0
    private var pcmScheduledFrames = 0
    private var pcmPlayedFrames = 0
    private var overflowEvents = 0
    private var droppedFrames = 0
    private var underrunEvents = 0
    private var invalidOrIncompleteFrameCount = 0
    private var sequenceGapCount = 0
    private var minScheduledLeadMs = Int.max
    private var maxScheduledLeadMs = 0
    private var maxQueueAgeMs = 0
    private var lastSequence: Int?
    private var responseSummaryLogged = false
    private var overflowLogged = false
    private var lastCompletionAt = Date()
    private var engineRestartCount = 0
    private var stallRestartStreak = 0
    private var stallWatchdog: DispatchSourceTimer?
    // (scheduledAt, outputFrames, sourceFrames) per outstanding buffer, FIFO.
    private var outstanding: [(Date, Int, Int)] = []

    // Synchronous mirrors used by UI and the microphone callback.
    private let stateLock = NSLock()
    private var mirroredPendingFrames = 0
    private var mirroredPlayedFrames = 0
    private var mirroredSpeaking = false
    private var captureMuteUntil = Date.distantPast
    private var lastAssistantChunkAt = Date.distantPast
    private var referencePCM = Data()
    private let referenceKeepBytes = 16_000 * 2 * 4

    override init() {
        super.init()
        audioQueue.setSpecific(key: queueKey, value: 1)
    }

    var isPlaying: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return mirroredSpeaking
    }

    var pendingFramesPublic: Int {
        stateLock.lock()
        defer { stateLock.unlock() }
        return mirroredPendingFrames
    }

    /// True only while speaker frames are queued, plus the acoustic tail.
    /// Safe for the realtime microphone callback.
    var shouldMuteCapture: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return mirroredSpeaking || Date() < captureMuteUntil
    }

    func playbackSnapshot() -> PlaybackSnapshot {
        stateLock.lock()
        let pcm = referencePCM
        let speaking = mirroredSpeaking
        let pending = mirroredPendingFrames
        let played = mirroredPlayedFrames
        let tail = Date() < captureMuteUntil
        let lastChunkAge = Date().timeIntervalSince(lastAssistantChunkAt)
        stateLock.unlock()
        return PlaybackSnapshot(
            pcm16: pcm,
            rms: Self.pcm16RMS(pcm),
            audible: speaking,
            echoGate: speaking || tail,
            playedMs: Int(Double(played) * 1000 / playerFormat.sampleRate),
            queuedMs: Int(Double(pending) * 1000 / playerFormat.sampleRate),
            assistantEpisodeActive: speaking || lastChunkAge < 2.5
        )
    }

    struct PlaybackMetrics: Sendable {
        let pcmReceivedFrames: Int
        let pcmScheduledFrames: Int
        let pcmPlayedFrames: Int
        let overflowEvents: Int
        let droppedFrames: Int
        let underrunEvents: Int
        let minScheduledLeadMs: Int
        let maxScheduledLeadMs: Int
        let currentScheduledLeadMs: Int
        let maxQueueAgeMs: Int
        let invalidFrameCount: Int
        let sequenceGapCount: Int
    }

    func metrics() -> PlaybackMetrics {
        syncOnAudioQueue {
            PlaybackMetrics(
                pcmReceivedFrames: pcmReceivedFrames,
                pcmScheduledFrames: pcmScheduledFrames,
                pcmPlayedFrames: pcmPlayedFrames,
                overflowEvents: overflowEvents,
                droppedFrames: droppedFrames,
                underrunEvents: underrunEvents,
                minScheduledLeadMs: minScheduledLeadMs == Int.max ? 0 : minScheduledLeadMs,
                maxScheduledLeadMs: maxScheduledLeadMs,
                currentScheduledLeadMs: scheduledLeadMs(),
                maxQueueAgeMs: maxQueueAgeMs,
                invalidFrameCount: invalidOrIncompleteFrameCount,
                sequenceGapCount: sequenceGapCount
            )
        }
    }

    /// Configure the one playback graph once for the active voice session.
    func beginVoiceSession() {
        audioQueue.async { [weak self] in
            guard let self else { return }
            beginSessionOnQueue(ephemeral: false)
        }
    }

    /// Stop all output and the output engine when Evie sleeps/closes.
    func endVoiceSession() {
        syncOnAudioQueue {
            sessionActive = false
            liveSessionOwned = false
            ephemeralSession = false
            invalidatePlayback(echoTail: false)
            if engine.isRunning { engine.stop() }
            stallWatchdog?.cancel()
            stallWatchdog = nil
        }
    }

    private func beginSessionOnQueue(ephemeral: Bool) {
        sessionActive = true
        if !ephemeral { liveSessionOwned = true }
        do {
            try ensureEngine()
        } catch {
            sessionActive = false
            ephemeralSession = false
            invalidatePlayback(echoTail: false)
            return
        }
        startStallWatchdog()
    }

    /// Completion-truth stall watchdog. AVAudioEngine can silently stop
    /// rendering on device reconfiguration while reporting isRunning —
    /// buffers stay queued, completions never fire, playback freezes mid-word
    /// while the producer keeps filling the queue. Truth is the completion
    /// stream itself: if audio is scheduled and the newest completion is stale,
    /// restart the engine once. Cheap, on-queue, no locks.
    private func startStallWatchdog() {
        guard stallWatchdog == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        timer.schedule(deadline: .now() + 1.0, repeating: 0.5)
        timer.setEventHandler { [weak self] in
            self?.audioQueue.async { [weak self] in
                guard let self, sessionActive, !responseFinished else { return }
                guard pendingBuffers > 0, scheduledLeadMs() >= 300 else { return }
                guard Date().timeIntervalSince(lastCompletionAt) > 1.2 else { return }
                // Give up if repeated restarts are not restoring completions;
                // an endless restart storm is worse than a frozen tail.
                guard stallRestartStreak < 5 else { return }
                stallRestartStreak += 1
                restartEngineOnQueue(reason: "stall")
            }
        }
        timer.resume()
        stallWatchdog = timer
    }

    private func restartEngineOnQueue(reason: String) {
        engineRestartCount += 1
        NSLog("EV_TTS[engine-restart reason=\(reason)] restarts=\(engineRestartCount)")
        if engine.isRunning { engine.stop() }
        engine.prepare()
        try? engine.start()
        // After an engine restart the PlayerNode must be told to play again,
        // otherwise queued buffers sit silent and the watchdog refires forever.
        if pendingBuffers > 0 {
            playerStarted = true
            playerNode.play()
        }
        lastCompletionAt = Date()
    }

    /// One-shot callers (fixtures, non-live TTS) own the engine only for the
    /// duration of their single response; it is stopped after playback.
    private func beginOneShotSessionIfNeeded() {
        syncOnAudioQueue {
            guard !liveSessionOwned, !sessionActive else { return }
            beginSessionOnQueue(ephemeral: true)
        }
    }

    /// Legacy call sites only use nil during teardown. Playback no longer
    /// attaches to a second/shared graph.
    func bind(to engine: AVAudioEngine?) {
        if engine == nil { endVoiceSession() }
    }

    /// Establish the only response permitted to enqueue PCM. Replacing it
    /// synchronously discards all stale aggregate and scheduled audio.
    func beginResponse(_ responseID: String) {
        guard !responseID.isEmpty else { return }
        syncOnAudioQueue {
            if activeResponseID != responseID || responseFinished {
                invalidatePlayback(echoTail: true)
                activeResponseID = responseID
                responseFinished = false
            }
        }
    }

    /// Decode and enqueue away from MainActor. No Task is created per delta.
    func enqueueBase64PCM(
        _ base64: String,
        contentType: String?,
        sampleRate: Double,
        responseID: String,
        sequence: Int? = nil
    ) {
        audioQueue.async { [weak self] in
            guard let self else { return }
            guard let data = Data(base64Encoded: base64), !data.isEmpty else {
                invalidOrIncompleteFrameCount += 1
                return
            }
            ingest(data, contentType: contentType, sampleRate: sampleRate, responseID: responseID, sequence: sequence)
        }
    }

    /// Used for fetched fixtures/container payloads after network I/O finishes.
    func enqueuePCM(
        _ data: Data,
        contentType: String?,
        sampleRate: Double,
        responseID: String,
        sequence: Int? = nil
    ) {
        audioQueue.async { [weak self] in
            self?.ingest(data, contentType: contentType, sampleRate: sampleRate, responseID: responseID, sequence: sequence)
        }
    }

    func finishResponse(_ responseID: String) {
        audioQueue.async { [weak self] in
            self?.finishResponseOnQueue(responseID)
        }
    }

    func cancelResponse(_ responseID: String?) {
        syncOnAudioQueue {
            guard responseID == nil || activeResponseID == responseID else { return }
            invalidatePlayback(echoTail: true)
        }
    }

    func stop() {
        cancelResponse(nil)
    }

    func stopForBargeIn() {
        syncOnAudioQueue { invalidatePlayback(echoTail: false) }
    }

    func recover() {
        syncOnAudioQueue {
            invalidatePlayback(echoTail: false)
            if engine.isRunning { engine.stop() }
        }
    }

    func prepareForNewTurn() {
        cancelResponse(nil)
    }

    func noteAssistantAudioComplete() {
        audioQueue.async { [weak self] in
            guard let self, let responseID = activeResponseID else { return }
            finishResponseOnQueue(responseID)
        }
    }

    func play(data: Data) throws {
        stop()
        try enqueue(data)
    }

    /// Non-Realtime callers may supply raw PCM16LE mono or PCM16 mono WAV.
    /// Compressed/container bytes are rejected rather than sent to speakers.
    func enqueue(_ data: Data, contentType: String? = nil, sampleRate: Int = 16_000) throws {
        if Self.isUnsupportedContainer(data, contentType: contentType) {
            throw NSError(
                domain: "EVTTS",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "unsupported audio container; expected PCM16LE mono or PCM WAV"]
            )
        }
        beginOneShotSessionIfNeeded()
        let responseID = UUID().uuidString
        beginResponse(responseID)
        enqueuePCM(data, contentType: contentType, sampleRate: Double(sampleRate), responseID: responseID)
        finishResponse(responseID)
    }

    // Listener Presence is cancelled. Keep the source-compatible surface as
    // a silent no-op so no auxiliary player node or competing lane exists.
    enum PlaybackRole { case normalAssistantResponse, listenerBackchannel }
    enum AuxCompletionPolicy { case finishDespiteOwnerSpeech }
    var listenerFeedbackQueuedFrames: Int { 0 }
    var listenerFeedbackAccounting: (started: Int, completed: Int, preempted: Int, dropped: Int) {
        (0, 0, 0, 0)
    }
    func enqueueListenerFeedback(
        _ pcm16: Data,
        sampleRate: Double = 16_000,
        gain: Float = 0.34,
        role: PlaybackRole = .listenerBackchannel,
        completionPolicy: AuxCompletionPolicy = .finishDespiteOwnerSpeech
    ) throws {
        _ = (pcm16, sampleRate, gain, role, completionPolicy)
    }
    func preemptListenerFeedbackForResponse(reason: String) { _ = reason }

    private func ingest(
        _ data: Data,
        contentType: String?,
        sampleRate declaredRate: Double,
        responseID: String,
        sequence: Int?
    ) {
        guard sessionActive, activeResponseID == responseID, !responseFinished else { return }
        if let seq = sequence {
            if let last = lastSequence, seq != last + 1 {
                sequenceGapCount += 1
            }
            lastSequence = seq
        }

        let pcm: Data
        let rate: Double
        if let wav = wavPCM(data) {
            pcm = wav.samples
            rate = wav.sampleRate
        } else {
            guard Self.isRawPCM(contentType: contentType, data: data),
                  declaredRate.isFinite,
                  (8_000...96_000).contains(declaredRate)
            else {
                invalidOrIncompleteFrameCount += 1
                return
            }
            pcm = data
            rate = declaredRate
        }

        guard let establishedRate = sourceRate else {
            sourceRate = rate
            converter = nil
            converterSourceRate = nil
            ingestAlignedPCM(pcm, rate: rate)
            return
        }
        guard abs(establishedRate - rate) < 0.5 else {
            invalidOrIncompleteFrameCount += 1
            return
        }
        ingestAlignedPCM(pcm, rate: establishedRate)
    }

    private func ingestAlignedPCM(_ pcm: Data, rate: Double) {
        receivedPCMBytes += pcm.count
        let incomingFrames = pcm.count / Self.sourceBytesPerFrame
        pcmReceivedFrames += incomingFrames
        stateLock.lock()
        lastAssistantChunkAt = Date()
        stateLock.unlock()

        var bytes = Data()
        if !partialFrameBytes.isEmpty {
            bytes.append(partialFrameBytes)
            partialFrameBytes.removeAll(keepingCapacity: true)
        }
        bytes.append(pcm)
        let alignedCount = bytes.count - (bytes.count % Self.sourceBytesPerFrame)
        if alignedCount < bytes.count {
            partialFrameBytes.append(bytes.suffix(bytes.count - alignedCount))
        }
        guard alignedCount > 0 else { return }

        // HARD CEILING ONLY. Speech is never dropped in normal operation: the
        // target-lead gate below absorbs producer jitter. Reaching the ceiling
        // means producer outran playback by >1.5s — counted loudly, and only
        // the excess beyond the ceiling is rejected (never already-accepted
        // audio, never the response).
        let incomingMs = Double(alignedCount / Self.sourceBytesPerFrame) * 1000 / rate
        let backlogMs = Double(totalBacklogMs())
        let headroomMs = Double(Self.hardCeilingMs) - backlogMs
        if incomingMs > headroomMs {
            overflowEvents += 1
            if headroomMs <= 0 {
                droppedFrames += alignedCount / Self.sourceBytesPerFrame
                logOverflowOnce()
                return
            }
            let allowedFrames = Int(headroomMs * rate / 1000)
            let allowedBytes = allowedFrames * Self.sourceBytesPerFrame
            droppedFrames += (alignedCount - allowedBytes) / Self.sourceBytesPerFrame
            aggregatePCM.append(bytes.prefix(allowedBytes))
            rememberPlaybackReference(Data(bytes.prefix(allowedBytes)))
        } else {
            aggregatePCM.append(bytes.prefix(alignedCount))
            rememberPlaybackReference(Data(bytes.prefix(alignedCount)))
        }
        drainAggregated(rate: rate)
        maybeStartPlayback()
    }

    /// Schedule full aggregated blocks while the PlayerNode-scheduled lead is
    /// below the steady target. The gate reads SCHEDULED audio only — the
    /// aggregate is a waiting room, not scheduled audio; counting it here
    /// would stall scheduling forever once it exceeded the target. `force`
    /// bypasses the gate (response tail: everything remaining must be
    /// scheduled, including the sub-block remainder).
    private func drainAggregated(rate: Double, force: Bool = false) {
        let targetBytes = Int(rate * Double(Self.sourceBytesPerFrame) * Double(Self.aggregationMs) / 1000)
        let alignedTarget = max(Self.sourceBytesPerFrame, targetBytes - targetBytes % Self.sourceBytesPerFrame)
        while aggregatePCM.count >= alignedTarget || (force && aggregatePCM.count > 0) {
            if !force, scheduledLeadMs() >= Self.targetLeadMs { break }
            let take = min(alignedTarget, aggregatePCM.count)
            let chunk = Data(aggregatePCM.prefix(take))
            aggregatePCM.removeFirst(take)
            schedulePCM(chunk, sourceRate: rate)
        }
    }

    private func maybeStartPlayback() {
        guard !playerStarted, pendingBuffers > 0 else { return }
        guard responseFinished || scheduledLeadMs() >= Self.startupPrebufferMs else { return }
        playerStarted = true
        playerNode.play()
    }

    private func schedulePCM(_ pcm: Data, sourceRate: Double) {
        let sourceFrames = pcm.count / Self.sourceBytesPerFrame
        guard sourceFrames > 0,
              pcm.count == sourceFrames * Self.sourceBytesPerFrame,
              let buffer = playbackBuffer(from: pcm, sourceRate: sourceRate),
              buffer.frameLength > 0
        else {
            invalidOrIncompleteFrameCount += 1
            return
        }
        do {
            try ensureEngine()
        } catch {
            invalidOrIncompleteFrameCount += 1
            return
        }

        let outFrames = Int(buffer.frameLength)
        let generation = streamGeneration
        pendingBuffers += 1
        pendingFrames += outFrames
        pcmScheduledFrames += sourceFrames
        outstanding.append((Date(), outFrames, sourceFrames))
        noteLeadSample()
        updateMirrors(speaking: true)
        setSpeaking(true)

        let sourceFramesForCallback = sourceFrames
        playerNode.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            self?.audioQueue.async { [weak self] in
                guard let self, streamGeneration == generation else { return }
                lastCompletionAt = Date()
                stallRestartStreak = 0
                // Completions fire in FIFO schedule order; pop unconditionally
                // and use the entry only for age truth. A frame-count mismatch
                // must never corrupt the outstanding queue.
                if !outstanding.isEmpty {
                    let entry = outstanding.removeFirst()
                    if entry.1 == outFrames {
                        let ageMs = Date().timeIntervalSince(entry.0) * 1000
                        if ageMs > Double(maxQueueAgeMs) { maxQueueAgeMs = Int(ageMs) }
                    }
                }
                pendingBuffers = max(0, pendingBuffers - 1)
                pendingFrames = max(0, pendingFrames - outFrames)
                pcmPlayedFrames += sourceFramesForCallback
                // Refill from the aggregate as the lead dips below target —
                // before the underrun check, so held audio prevents false
                // underruns.
                if let rate = self.sourceRate { drainAggregated(rate: rate) }
                // Near-starvation: schedule a partial remainder rather than
                // let the PlayerNode run dry while ordered audio still waits
                // in the aggregate. Ordered, contiguous, non-duplicated.
                if pendingBuffers == 0, let rate = self.sourceRate, !aggregatePCM.isEmpty {
                    let chunk = aggregatePCM
                    aggregatePCM.removeAll(keepingCapacity: true)
                    schedulePCM(chunk, sourceRate: rate)
                }
                maybeStartPlayback()
                if pendingBuffers == 0 {
                    if !responseFinished { underrunEvents += 1 }
                    setSpeaking(false)
                    if responseFinished, !responseSummaryLogged {
                        responseSummaryLogged = true
                        logCounters(reason: "response-complete")
                    }
                    if responseFinished, ephemeralSession, !liveSessionOwned {
                        ephemeralSession = false
                        sessionActive = false
                        if engine.isRunning { engine.stop() }
                    }
                } else {
                    updateMirrors(speaking: true)
                }
            }
        }
    }

    private func noteLeadSample() {
        let lead = scheduledLeadMs()
        if lead < minScheduledLeadMs { minScheduledLeadMs = lead }
        if lead > maxScheduledLeadMs { maxScheduledLeadMs = lead }
    }

    /// Scheduled lead in ms: audio physically queued on the PlayerNode (48k).
    /// This is the continuity truth — when it reaches 0 during active speech
    /// the speakers underrun.
    private func scheduledLeadMs() -> Int {
        Int(Double(pendingFrames) * 1000 / playerFormat.sampleRate)
    }

    /// Total backlog: scheduled lead plus still-unaggregated source PCM. This
    /// is the latency the owner would experience; used for the ceiling only.
    private func totalBacklogMs() -> Int {
        guard let rate = sourceRate, rate > 0 else { return scheduledLeadMs() }
        let aggregateFrames = aggregatePCM.count / Self.sourceBytesPerFrame
        return scheduledLeadMs() + Int(Double(aggregateFrames) * 1000 / rate)
    }

    private func logOverflowOnce() {
        guard !overflowLogged else { return }
        overflowLogged = true
        logCounters(reason: "overflow-ceiling")
    }

    private func logCounters(reason: String) {
        let receivedMs = durationMs(frames: pcmReceivedFrames)
        let scheduledMs = durationMs(frames: pcmScheduledFrames)
        let playedMs = durationMs(frames: pcmPlayedFrames)
        NSLog(
            "EV_TTS[\(reason)] received=\(receivedMs)ms scheduled=\(scheduledMs)ms played=\(playedMs)ms lead(min/max/cur)=\(minScheduledLeadMs == Int.max ? 0 : minScheduledLeadMs)/\(maxScheduledLeadMs)/\(scheduledLeadMs())ms backlog=\(totalBacklogMs())ms queueAgeMax=\(maxQueueAgeMs)ms overflow=\(overflowEvents) dropped=\(droppedFrames) underruns=\(underrunEvents) gaps=\(sequenceGapCount) invalid=\(invalidOrIncompleteFrameCount) restarts=\(engineRestartCount)"
        )
    }

    private func durationMs(frames: Int) -> Int {
        guard let rate = sourceRate, rate > 0 else { return 0 }
        return Int(Double(frames) * 1000 / rate)
    }

    private func ensureEngine() throws {
        if !engineConfigured {
            engine.isAutoShutdownEnabled = false
            engine.attach(playerNode)
            engine.connect(playerNode, to: engine.mainMixerNode, format: playerFormat)
            engine.mainMixerNode.outputVolume = 1.0
            playerNode.volume = 1.0
            // Device reconfiguration (route change, virtual-driver flap) can
            // stop the engine mid-response. Recover on the audio queue.
            NotificationCenter.default.addObserver(
                forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
            ) { [weak self] _ in
                self?.audioQueue.async { [weak self] in
                    guard let self, sessionActive else { return }
                    restartEngineOnQueue(reason: "config-change")
                }
            }
            engineConfigured = true
        }
        if !engine.isRunning {
            engine.prepare()
            try engine.start()
        }
    }

    private func invalidatePlayback(echoTail: Bool) {
        streamGeneration += 1
        playerNode.stop()
        playerNode.reset()
        activeResponseID = nil
        responseFinished = false
        playerStarted = false
        sourceRate = nil
        partialFrameBytes.removeAll(keepingCapacity: true)
        aggregatePCM.removeAll(keepingCapacity: true)
        converter = nil
        converterSourceRate = nil
        pendingBuffers = 0
        pendingFrames = 0
        outstanding = []
        lastSequence = nil
        responseSummaryLogged = false
        overflowLogged = false
        stallRestartStreak = 0
        receivedPCMBytes = 0
        pcmReceivedFrames = 0
        pcmScheduledFrames = 0
        pcmPlayedFrames = 0
        overflowEvents = 0
        droppedFrames = 0
        underrunEvents = 0
        sequenceGapCount = 0
        minScheduledLeadMs = Int.max
        maxScheduledLeadMs = 0
        maxQueueAgeMs = 0
        setSpeaking(false, echoTail: echoTail)
    }

    private func finishResponseOnQueue(_ responseID: String) {
        guard activeResponseID == responseID, !responseFinished else { return }
        responseFinished = true
        if !partialFrameBytes.isEmpty {
            // Sub-sample tail bytes cannot form a valid PCM frame; the provider
            // stream broke. Counted, never interpreted.
            invalidOrIncompleteFrameCount += 1
            partialFrameBytes.removeAll(keepingCapacity: true)
        }
        // §13: the final partial aggregate is a valid tail — flush everything
        // remaining as audio, bypassing the lead gate.
        if let rate = sourceRate {
            drainAggregated(rate: rate, force: true)
        }
        maybeStartPlayback()
        if pendingBuffers == 0 { setSpeaking(false) }
    }

    private func setSpeaking(_ speaking: Bool, echoTail: Bool = true) {
        updateMirrors(speaking: speaking, echoTail: echoTail)
        guard reportedPlaying != speaking else { return }
        reportedPlaying = speaking
        DispatchQueue.main.async { [weak self] in self?.onPlayingChange?(speaking) }
    }

    private func updateMirrors(speaking: Bool, echoTail: Bool = true) {
        stateLock.lock()
        mirroredPendingFrames = pendingFrames
        mirroredPlayedFrames = pcmPlayedFrames
        mirroredSpeaking = speaking
        if !speaking {
            captureMuteUntil = echoTail ? Date().addingTimeInterval(Self.echoTail) : .distantPast
        }
        stateLock.unlock()
    }

    private func playbackBuffer(from pcm: Data, sourceRate: Double) -> AVAudioPCMBuffer? {
        guard let source = floatBuffer(from: pcm, sampleRate: sourceRate) else { return nil }
        if abs(sourceRate - playerFormat.sampleRate) < 0.5 { return source }
        if converter == nil || converterSourceRate != sourceRate {
            converter = AVAudioConverter(from: source.format, to: playerFormat)
            converterSourceRate = sourceRate
        }
        guard let converter else { return nil }
        let ratio = playerFormat.sampleRate / sourceRate
        let capacity = AVAudioFrameCount(ceil(Double(source.frameLength) * ratio)) + 64
        guard let destination = AVAudioPCMBuffer(pcmFormat: playerFormat, frameCapacity: capacity) else {
            return nil
        }
        var error: NSError?
        var provided = false
        let status = converter.convert(to: destination, error: &error) { _, inputStatus in
            if provided {
                inputStatus.pointee = .noDataNow
                return nil
            }
            provided = true
            inputStatus.pointee = .haveData
            return source
        }
        guard status != .error, error == nil, destination.frameLength > 0 else { return nil }
        return destination
    }

    private func floatBuffer(from pcm: Data, sampleRate: Double) -> AVAudioPCMBuffer? {
        let frames = pcm.count / Self.sourceBytesPerFrame
        guard frames > 0, pcm.count == frames * Self.sourceBytesPerFrame else { return nil }
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false
        ), let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
           let destination = buffer.floatChannelData?[0]
        else { return nil }
        buffer.frameLength = AVAudioFrameCount(frames)
        pcm.withUnsafeBytes { raw in
            let bytes = raw.bindMemory(to: UInt8.self)
            for index in 0..<frames {
                let bits = UInt16(bytes[index * 2]) | UInt16(bytes[index * 2 + 1]) << 8
                destination[index] = Float(Int16(bitPattern: bits)) / 32_768
            }
        }
        return buffer
    }

    private func rememberPlaybackReference(_ pcm: Data) {
        stateLock.lock()
        referencePCM.append(pcm)
        if referencePCM.count > referenceKeepBytes {
            referencePCM.removeFirst(referencePCM.count - referenceKeepBytes)
        }
        stateLock.unlock()
        VoiceLevelMeter.shared.ingestOutputPCM16(pcm)
    }

    private func syncOnAudioQueue<T>(_ work: () -> T) -> T {
        if DispatchQueue.getSpecific(key: queueKey) != nil { return work() }
        return audioQueue.sync(execute: work)
    }

    private static func isRawPCM(contentType: String?, data: Data) -> Bool {
        let kind = (contentType ?? "").lowercased()
        if kind.contains("pcm") || kind.contains("l16") || kind == "audio/raw" { return true }
        if !kind.isEmpty { return false }
        return !isUnsupportedContainer(data, contentType: contentType)
    }

    private static func isUnsupportedContainer(_ data: Data, contentType: String?) -> Bool {
        let kind = (contentType ?? "").lowercased()
        if kind.contains("wav") || data.starts(with: Data("RIFF".utf8)) { return false }
        if kind.contains("mpeg") || kind.contains("mp3") || kind.contains("mp4")
            || kind.contains("aac") || kind.contains("ogg") || kind.contains("flac")
        { return true }
        if data.starts(with: Data("ID3".utf8)) || data.starts(with: Data("OggS".utf8))
            || data.starts(with: Data("fLaC".utf8))
        { return true }
        if data.count >= 2, data[0] == 0xFF, data[1] & 0xE0 == 0xE0 { return true }
        return false
    }

    private static func pcm16RMS(_ pcm: Data) -> Float {
        let count = pcm.count / 2
        guard count > 0 else { return 0 }
        var sum: Float = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for sample in samples.prefix(count) {
                let value = Float(Int16(littleEndian: sample)) / 32_768
                sum += value * value
            }
        }
        return sqrt(sum / Float(count))
    }
}

private struct WavPCM {
    let samples: Data
    let sampleRate: Double
}

private func wavPCM(_ data: Data) -> WavPCM? {
    guard data.count >= 44,
          data.starts(with: Data("RIFF".utf8)),
          String(data: data.subdata(in: 8..<12), encoding: .ascii) == "WAVE"
    else { return nil }
    var offset = 12
    var formatTag: UInt16 = 0
    var sampleRate = 0.0
    var bits: UInt16 = 0
    var channels: UInt16 = 0
    var blockAlign: UInt16 = 0
    var payload: Data?
    while offset + 8 <= data.count {
        let id = String(data: data.subdata(in: offset..<(offset + 4)), encoding: .ascii) ?? ""
        let size = Int(u32LE(data, offset + 4))
        let start = offset + 8
        guard size >= 0, start <= data.count, size <= data.count - start else { return nil }
        let end = start + size
        if id == "fmt ", size >= 16 {
            formatTag = u16LE(data, start)
            channels = u16LE(data, start + 2)
            sampleRate = Double(u32LE(data, start + 4))
            blockAlign = u16LE(data, start + 12)
            bits = u16LE(data, start + 14)
        } else if id == "data" {
            payload = data.subdata(in: start..<end)
        }
        offset = end + (size % 2)
    }
    guard let payload,
          formatTag == 1,
          bits == 16,
          channels == 1,
          blockAlign == 2,
          sampleRate.isFinite,
          (8_000...96_000).contains(sampleRate)
    else { return nil }
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
