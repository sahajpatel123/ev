import AVFoundation
import Foundation
import os.lock

/// True continuous playback: network producer → ring → hardware-clocked SourceNode.
/// All hot-path PCM flows off MainActor on this actor's executor.
actor PlaybackCoordinator {
    static let shared = PlaybackCoordinator()
    nonisolated(unsafe) static var isPlayingSync = false

    // MARK: Ring buffer (SPSC, lock-free for render)
    private final class RingBuffer {
        let capacity: Int // frames
        let ptr: UnsafeMutablePointer<Float>
        var writeIndex: Int = 0
        var readIndex: Int = 0
        var available: Int = 0
        var lock = os_unfair_lock()
        var underruns: Int = 0
        var overruns: Int = 0

        init(capacityFrames: Int) {
            capacity = capacityFrames
            ptr = .allocate(capacity: capacity)
            ptr.initialize(repeating: 0, count: capacity)
        }
        deinit { ptr.deallocate() }

        // Producer: write Float32 mono, returns frames written or 0 if overrun
        func write(_ src: UnsafePointer<Float>, frames: Int) -> Int {
            os_unfair_lock_lock(&lock)
            defer { os_unfair_lock_unlock(&lock) }
            if available + frames > capacity {
                overruns += 1
                return 0
            }
            let firstPart = min(frames, capacity - writeIndex)
            ptr.advanced(by: writeIndex).update(from: src, count: firstPart)
            if frames > firstPart {
                ptr.update(from: src.advanced(by: firstPart), count: frames - firstPart)
            }
            writeIndex = (writeIndex + frames) % capacity
            available += frames
            return frames
        }

        // Consumer: try to read, returns frames read (0 if not enough and we output silence)
        // Called from render thread with trylock.
        func tryRead(into dst: UnsafeMutablePointer<Float>, frames: Int) -> Int {
            if !os_unfair_lock_trylock(&lock) { return 0 }
            defer { os_unfair_lock_unlock(&lock) }
            if available < frames {
                underruns += 1
                return 0
            }
            let firstPart = min(frames, capacity - readIndex)
            dst.update(from: ptr.advanced(by: readIndex), count: firstPart)
            if frames > firstPart {
                dst.advanced(by: firstPart).update(from: ptr, count: frames - firstPart)
            }
            readIndex = (readIndex + frames) % capacity
            available -= frames
            return frames
        }

        func clear() {
            os_unfair_lock_lock(&lock)
            writeIndex = 0
            readIndex = 0
            available = 0
            os_unfair_lock_unlock(&lock)
        }

        var bufferedFrames: Int {
            os_unfair_lock_lock(&lock)
            defer { os_unfair_lock_unlock(&lock) }
            return available
        }
    }

    // MARK: Engine and nodes
    private let engine = AVAudioEngine()
    private var sourceNode: AVAudioSourceNode!
    private var ring: RingBuffer!
    private var outputFormat: AVAudioFormat!

    // Jitter policy
    private let targetMs: Double = 250
    private let lowWaterMs: Double = 120
    private let highWaterMs: Double = 350
    private let hardMaxMs: Double = 450
    private var isPrimed = false
    private var streamGeneration = 0

    // Converter (producer side, not render)
    private var converter: AVAudioConverter?
    private var converterSourceRate: Double?

    // Diagnostics
    private var receivedFrames: Int = 0
    private var convertedFrames: Int = 0
    private var renderedFrames: Int = 0
    private var converterResets = 0
    private var lastRate: Double = 16000

    // For UI
    private var _isPlaying = false

    init() {
        let capacityFrames = Int(48000 * 3.0) // 3s at 48k
        ring = RingBuffer(capacityFrames: capacityFrames)
        // Output format: query actual HAL after engine start, fallback to 48k mono
        let fmt = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
        outputFormat = fmt
        sourceNode = AVAudioSourceNode(format: fmt) { [weak self] _, _, frameCount, audioBufferList -> OSStatus in
            guard let self else { return noErr }
            let ablPointer = UnsafeMutableAudioBufferListPointer(audioBufferList)
            guard let buf = ablPointer.first, let dst = buf.mData?.assumingMemoryBound(to: Float.self) else { return noErr }
            // If not primed, output silence
            let ring = self.ring!
            // Try to read
            let framesRead = ring.tryRead(into: dst, frames: Int(frameCount))
            if framesRead < Int(frameCount) {
                // Fill remainder with silence
                let remaining = Int(frameCount) - framesRead
                if remaining > 0 {
                    dst.advanced(by: framesRead).initialize(repeating: 0, count: remaining)
                }
                // If we were primed and now starved, count underrun (already in ring)
            }
            // Zero out any extra channels (should be mono)
            return noErr
        }
        engine.attach(sourceNode)
        engine.connect(sourceNode, to: engine.mainMixerNode, format: fmt)
        // Keep mixer at unity
        engine.mainMixerNode.outputVolume = 1.0
        try? engine.start()
        // Observe output changes, but do not tear down during speech
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            Task { await self?.handleOutputConfigChange() }
        }
    }

    private func handleOutputConfigChange() {
        // Only rebuild output graph if not speaking and ring is empty
        if _isPlaying { return }
        // For now, just ensure engine is running
        if !engine.isRunning {
            try? engine.start()
        }
    }

    // MARK: Public API

    func setPlayer(_ player: TTSPlayer) {
        // Kept for compatibility: AppModel still owns a TTSPlayer for UI.
        // No-op: coordinator is now authoritative.
    }

    func enqueue(pcm: Data, sampleRate: Double) {
        // Producer: convert Int16@sampleRate -> Float32@48k and write to ring
        guard pcm.count % 2 == 0 else { return }
        let framesIn = pcm.count / 2
        receivedFrames += framesIn
        lastRate = sampleRate
        // Convert
        let inFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true)!
        let outFormat = outputFormat!
        if converter == nil || converterSourceRate != sampleRate {
            converter = AVAudioConverter(from: inFormat, to: outFormat)
            converterSourceRate = sampleRate
            converterResets += 1
        }
        guard let conv = converter else { return }
        // Prepare input buffer
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(framesIn)) else { return }
        inBuf.frameLength = AVAudioFrameCount(framesIn)
        pcm.withUnsafeBytes { src in
            memcpy(inBuf.int16ChannelData![0], src.baseAddress!, pcm.count)
        }
        let ratio = outFormat.sampleRate / inFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(framesIn) * ratio * 1.2 + 16)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: outCapacity) else { return }
        var error: NSError?
        let status = conv.convert(to: outBuf, error: &error) { _, outStatus in
            outStatus.pointee = .haveData
            return inBuf
        }
        guard status != .error, let converted = outBuf, converted.frameLength > 0 else { return }
        let framesOut = Int(converted.frameLength)
        convertedFrames += framesOut
        // Write Float32 to ring
        let written = ring.write(converted.floatChannelData![0], frames: framesOut)
        if written == 0 {
            // Overrun: ring full, drop
            return
        }
        // Prime check: if not yet primed and we have targetMs buffered, mark primed
        if !isPrimed {
            let bufferedMs = Double(ring.bufferedFrames) / 48000 * 1000
            if bufferedMs >= targetMs {
                isPrimed = true
                _isPlaying = true
                Self.isPlayingSync = true
                // Ensure engine is playing (source node is always attached, engine running)
                if !engine.isRunning {
                    try? engine.start()
                }
            }
        }
    }

    func stop() {
        ring.clear()
        isPrimed = false
        _isPlaying = false
        Self.isPlayingSync = false
        streamGeneration += 1
        // Do not stop engine; keep it warm for next response. Just clear.
        // Engine remains running, source will output silence until re-primed.
    }

    var isPlaying: Bool { _isPlaying && ring.bufferedFrames > 0 }
    var pendingFrames: Int { ring.bufferedFrames }

    // Diagnostics snapshot for trace
    func snapshot() -> [String: Any] {
        let bufferedMs = Double(ring.bufferedFrames) / 48000 * 1000
        return [
            "receivedFrames": receivedFrames,
            "convertedFrames": convertedFrames,
            "bufferedFrames": ring.bufferedFrames,
            "bufferedMs": bufferedMs,
            "targetMs": targetMs,
            "lowWaterMs": lowWaterMs,
            "underruns": ring.underruns,
            "overruns": ring.overruns,
            "converterResets": converterResets,
            "isPrimed": isPrimed,
            "isPlaying": isPlaying,
            "outputRate": outputFormat.sampleRate,
            "outputChannels": outputFormat.channelCount,
        ]
    }
}
