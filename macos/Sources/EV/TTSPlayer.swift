import AVFoundation
import Foundation

/// Plays spoken audio. Live Grok Voice sends a stream of 16 kHz PCM frames;
/// those are scheduled on `AVAudioPlayerNode` so words do not restart a new
/// `AVAudioPlayer` (that gap is what made her sound laggy). Container files
/// (WAV / MP3 from push-to-talk) still use `AVAudioPlayer`.
final class TTSPlayer: NSObject, AVAudioPlayerDelegate {
    var onPlayingChange: ((Bool) -> Void)?

    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
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

    // Hold ~100 ms of converted audio before the first play() so a normal
    // network gap does not become an audible hole. Cap the wait so the
    // first word is not late.
    private let streamPrimeDelay: TimeInterval = 0.02
    private let minStartSeconds: TimeInterval = 0.10
    private let primeRetryDelay: TimeInterval = 0.01
    private let maxPrimeWait: TimeInterval = 0.12
    private var streamPrimeDeadline = Date.distantPast
    // Connect the player at 48 kHz, the usual Mac HAL rate. Scheduling 16 kHz
    // buffers on that graph (or returning the 16 kHz source when conversion
    // fails) plays the reply at the wrong speed. Convert with one streaming
    // converter and never schedule a mismatched format.
    private let playbackSampleRate: Double = 48_000
    private let captureEchoTail: TimeInterval = 0.14
    private var playbackFalseTask: DispatchWorkItem?

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
                    self.stopStream(notify: true)
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
        stopStream(notify: false)
        if filePlayer?.isPlaying == true {
            fileQueue.append(data)
            return
        }
        try playFile(data)
    }

    func stop() {
        let wasPlaying = isPlaying
        fileQueue.removeAll()
        filePlayer?.delegate = nil
        filePlayer?.stop()
        filePlayer = nil
        stopStream(notify: false)
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
        stopStream(notify: had)
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
        stop()
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
        lock.lock()
        streamGeneration += 1
        pendingBuffers = 0
        pendingFrames = 0
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        lock.unlock()
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
        lock.lock()
        streamGeneration += 1
        pendingBuffers = 0
        pendingFrames = 0
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        lock.unlock()
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
        VoiceLevelMeter.shared.ingestOutputPCM16(pcm)
        do {
            try schedulePCM(pcm, sampleRate: sampleRate)
        } catch {
            guard usingSharedEngine else { throw error }
            detachSharedPlayer()
            try schedulePCM(pcm, sampleRate: sampleRate)
        }
    }

    private func schedulePCM(_ pcm: Data, sampleRate: Double) throws {
        try ensureEngine(sampleRate: sampleRate)
        guard let buffer = playbackBuffer(from: pcm, sourceRate: sampleRate) else { return }
        let frameCount = Int(buffer.frameLength)
        lock.lock()
        pendingBuffers += 1
        pendingFrames += frameCount
        let generation = streamGeneration
        lock.unlock()
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
            let idle = self.pendingBuffers == 0
            if idle {
                self.captureMuteUntil = Date().addingTimeInterval(self.captureEchoTail)
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

    private func stopStream(notify: Bool) {
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
        let had = pendingBuffers > 0
        pendingBuffers = 0
        pendingFrames = 0
        captureMuteUntil = Date().addingTimeInterval(captureEchoTail)
        lock.unlock()
        resetPlaybackConverter()
        if notify, had {
            onPlayingChange?(false)
        }
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
