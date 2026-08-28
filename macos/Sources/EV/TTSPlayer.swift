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
    private static let maxQueuedMs = 480
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
    private var sourceRate: Double?
    private var partialFrameBytes = Data()
    private var aggregatePCM = Data()
    private var converter: AVAudioConverter?
    private var converterSourceRate: Double?
    private var pendingBuffers = 0
    private var pendingFrames = 0
    private var reportedPlaying = false

    // Minimal counters only.
    private var receivedPCMBytes = 0
    private var scheduledFrames = 0
    private var playedFrames = 0
    private var underrunCount = 0
    private var invalidOrIncompleteFrameCount = 0
    private var droppedOverflowFrames = 0

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
        let receivedPCMBytes: Int
        let scheduledFrames: Int
        let playedFrames: Int
        let queuedDurationMs: Int
        let underrunCount: Int
        let invalidOrIncompleteFrameCount: Int
        let droppedOverflowFrames: Int
    }

    func metrics() -> PlaybackMetrics {
        syncOnAudioQueue {
            PlaybackMetrics(
                receivedPCMBytes: receivedPCMBytes,
                scheduledFrames: scheduledFrames,
                playedFrames: playedFrames,
                queuedDurationMs: queuedDurationMs(),
                underrunCount: underrunCount,
                invalidOrIncompleteFrameCount: invalidOrIncompleteFrameCount,
                droppedOverflowFrames: droppedOverflowFrames
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
        }
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
        responseID: String
    ) {
        audioQueue.async { [weak self] in
            guard let self else { return }
            guard let data = Data(base64Encoded: base64), !data.isEmpty else {
                invalidOrIncompleteFrameCount += 1
                return
            }
            ingest(data, contentType: contentType, sampleRate: sampleRate, responseID: responseID)
        }
    }

    /// Used for fetched fixtures/container payloads after network I/O finishes.
    func enqueuePCM(
        _ data: Data,
        contentType: String?,
        sampleRate: Double,
        responseID: String
    ) {
        audioQueue.async { [weak self] in
            self?.ingest(data, contentType: contentType, sampleRate: sampleRate, responseID: responseID)
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
        responseID: String
    ) {
        guard sessionActive, activeResponseID == responseID, !responseFinished else { return }

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

        let aligned = bytes.prefix(alignedCount)
        let currentMs = queuedDurationMs()
        let availableMs = max(0, Self.maxQueuedMs - currentMs)
        let maxBytes = Int(rate * Double(Self.sourceBytesPerFrame) * Double(availableMs) / 1000)
        let allowedBytes = min(aligned.count, maxBytes - (maxBytes % Self.sourceBytesPerFrame))
        if allowedBytes < aligned.count {
            droppedOverflowFrames += (aligned.count - allowedBytes) / Self.sourceBytesPerFrame
        }
        guard allowedBytes > 0 else { return }
        aggregatePCM.append(aligned.prefix(allowedBytes))
        rememberPlaybackReference(Data(aligned.prefix(allowedBytes)))
        drainFullAggregates(rate: rate)
    }

    private func drainFullAggregates(rate: Double) {
        let targetBytes = Int(rate * Double(Self.sourceBytesPerFrame) * Double(Self.aggregationMs) / 1000)
        let alignedTarget = max(Self.sourceBytesPerFrame, targetBytes - targetBytes % Self.sourceBytesPerFrame)
        while aggregatePCM.count >= alignedTarget {
            let chunk = Data(aggregatePCM.prefix(alignedTarget))
            aggregatePCM.removeFirst(alignedTarget)
            schedulePCM(chunk, sourceRate: rate)
        }
    }

    private func flushAggregate() {
        guard let rate = sourceRate else { return }
        let alignedCount = aggregatePCM.count - aggregatePCM.count % Self.sourceBytesPerFrame
        guard alignedCount > 0 else { return }
        let chunk = Data(aggregatePCM.prefix(alignedCount))
        aggregatePCM.removeFirst(alignedCount)
        schedulePCM(chunk, sourceRate: rate)
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

        let frames = Int(buffer.frameLength)
        let generation = streamGeneration
        pendingBuffers += 1
        pendingFrames += frames
        scheduledFrames += frames
        updateMirrors(speaking: true)
        setSpeaking(true)

        playerNode.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            self?.audioQueue.async { [weak self] in
                guard let self, streamGeneration == generation else { return }
                pendingBuffers = max(0, pendingBuffers - 1)
                pendingFrames = max(0, pendingFrames - frames)
                playedFrames += frames
                if pendingBuffers == 0 {
                    if !responseFinished { underrunCount += 1 }
                    setSpeaking(false)
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
        playerNode.play()
    }

    private func ensureEngine() throws {
        if !engineConfigured {
            engine.isAutoShutdownEnabled = false
            engine.attach(playerNode)
            engine.connect(playerNode, to: engine.mainMixerNode, format: playerFormat)
            engine.mainMixerNode.outputVolume = 1.0
            playerNode.volume = 1.0
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
        sourceRate = nil
        partialFrameBytes.removeAll(keepingCapacity: true)
        aggregatePCM.removeAll(keepingCapacity: true)
        converter = nil
        converterSourceRate = nil
        pendingBuffers = 0
        pendingFrames = 0
        setSpeaking(false, echoTail: echoTail)
    }

    private func finishResponseOnQueue(_ responseID: String) {
        guard activeResponseID == responseID, !responseFinished else { return }
        responseFinished = true
        if !partialFrameBytes.isEmpty {
            invalidOrIncompleteFrameCount += 1
            partialFrameBytes.removeAll(keepingCapacity: true)
        }
        flushAggregate()
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
        mirroredPlayedFrames = playedFrames
        mirroredSpeaking = speaking
        if !speaking {
            captureMuteUntil = echoTail ? Date().addingTimeInterval(Self.echoTail) : .distantPast
        }
        stateLock.unlock()
    }

    private func queuedDurationMs() -> Int {
        let scheduledMs = Int(Double(pendingFrames) * 1000 / playerFormat.sampleRate)
        guard let rate = sourceRate, rate > 0 else { return scheduledMs }
        let aggregateFrames = aggregatePCM.count / Self.sourceBytesPerFrame
        return scheduledMs + Int(Double(aggregateFrames) * 1000 / rate)
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
