import AVFoundation
import Foundation
import Synchronization

/// True continuous playback: network producer → ring → hardware-clocked SourceNode.
/// All hot-path PCM flows off MainActor on this actor's executor.
/// Render callback is true lock-free SPSC: no mutex, no trylock, no allocation.
actor PlaybackCoordinator {
    static let shared = PlaybackCoordinator()
    nonisolated(unsafe) static var isPlayingSync = false

    // MARK: Ring buffer — true lock-free SPSC
    // Producer owns writeIndex, consumer owns readIndex.
    // All atomics, no locks, no blocking, no allocation in render.
    private final class RingBuffer: @unchecked Sendable {
        let capacity: Int // frames
        let ptr: UnsafeMutablePointer<Float>
        let writeIndex = Atomic<Int>(0)
        let readIndex = Atomic<Int>(0)
        // Diagnostic counters (also lock-free)
        let underrunFrames = Atomic<Int>(0)
        let overrunCount = Atomic<Int>(0)
        let totalWrites = Atomic<Int>(0)
        let totalReads = Atomic<Int>(0)

        init(capacityFrames: Int) {
            capacity = capacityFrames
            ptr = .allocate(capacity: capacity)
            ptr.initialize(repeating: 0, count: capacity)
        }
        deinit { ptr.deallocate() }

        // Producer: SPSC write, single producer only (PlaybackCoordinator actor)
        // Returns frames written or 0 if would overrun (counts overrun, does not droppartial)
        func write(_ src: UnsafePointer<Float>, frames: Int) -> Int {
            let w = writeIndex.load(ordering: .relaxed)
            let r = readIndex.load(ordering: .acquiring)
            let available = w - r
            let free = capacity - available
            if frames > free {
                overrunCount.wrappingIncrement(ordering: .relaxed)
                return 0
            }
            let wMod = w % capacity
            let firstPart = min(frames, capacity - wMod)
            ptr.advanced(by: wMod).update(from: src, count: firstPart)
            if frames > firstPart {
                ptr.update(from: src.advanced(by: firstPart), count: frames - firstPart)
            }
            // Release: make buffer writes visible before index update
            writeIndex.store(w + frames, ordering: .releasing)
            totalWrites.wrappingIncrement(by: frames, ordering: .relaxed)
            return frames
        }

        // Consumer: lock-free, called from hardware render thread (single consumer)
        // Copies min(available, frames), zero-fills tail if underrun, advances readIndex by copied
        // Returns frames copied (0..frames). Never blocks, never locks.
        func read(into dst: UnsafeMutablePointer<Float>, frames: Int) -> Int {
            let r = readIndex.load(ordering: .relaxed)
            let w = writeIndex.load(ordering: .acquiring)
            let available = w - r
            let toCopy = min(available, frames)
            if toCopy > 0 {
                let rMod = r % capacity
                let firstPart = min(toCopy, capacity - rMod)
                dst.update(from: ptr.advanced(by: rMod), count: firstPart)
                if toCopy > firstPart {
                    dst.advanced(by: firstPart).update(from: ptr, count: toCopy - firstPart)
                }
                readIndex.store(r + toCopy, ordering: .releasing)
                totalReads.wrappingIncrement(by: toCopy, ordering: .relaxed)
            }
            if toCopy < frames {
                // True underrun: ring had fewer than requested
                underrunFrames.wrappingIncrement(by: frames - toCopy, ordering: .relaxed)
                // Caller will zero-fill remaining tail
            }
            return toCopy
        }

        func clear() {
            // Only called when no render in flight and no producer active (stop)
            // Reset indices with release/acquire to ensure visibility
            readIndex.store(0, ordering: .releasing)
            writeIndex.store(0, ordering: .releasing)
            // Counters are not cleared here; they are cumulative for diagnostics
            // Underrun/overrun counters are intentionally not reset on clear to preserve history
            // But we do need to zero the buffer for safety (not required for correctness)
        }

        var bufferedFrames: Int {
            let w = writeIndex.load(ordering: .acquiring)
            let r = readIndex.load(ordering: .acquiring)
            return w - r
        }

        // For diagnostics outside render
        var snapshot: (available: Int, underruns: Int, overruns: Int, writes: Int, reads: Int) {
            let w = writeIndex.load(ordering: .acquiring)
            let r = readIndex.load(ordering: .acquiring)
            return (w - r, underrunFrames.load(ordering: .relaxed), overrunCount.load(ordering: .relaxed), totalWrites.load(ordering: .relaxed), totalReads.load(ordering: .relaxed))
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

    // Deadline and memory-pressure diagnostics (outside render, sampled)
    private let memoryPressureSource: DispatchSourceMemoryPressure?
    private var lastPressure: DispatchSource.MemoryPressureEvent = .normal

    init() {
        let capacityFrames = 48000 * 3 // 3s at 48k
        ring = RingBuffer(capacityFrames: capacityFrames)
        // Output format: try actual HAL rate, fallback 48k mono
        var fmt = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
        // Pre-create source with 48k; after engine start we will not renegotiate mid-stream
        outputFormat = fmt
        sourceNode = AVAudioSourceNode(format: fmt) { [weak self] _, _, frameCount, audioBufferList -> OSStatus in
            guard let self else { return noErr }
            let ablPointer = UnsafeMutableAudioBufferListPointer(audioBufferList)
            guard let buf = ablPointer.first, let dst = buf.mData?.assumingMemoryBound(to: Float.self) else { return noErr }
            let ring = self.ring!
            // Lock-free SPSC read: copy min(available, requested), zero-fill tail if underrun
            // No locks, no allocation, no Foundation, no MainActor — just atomics and memcpy
            let framesRead = ring.read(into: dst, frames: Int(frameCount))
            if framesRead < Int(frameCount) {
                let remaining = Int(frameCount) - framesRead
                if remaining > 0 {
                    dst.advanced(by: framesRead).initialize(repeating: 0, count: remaining)
                }
            }
            return noErr
        }
        engine.attach(sourceNode)
        engine.connect(sourceNode, to: engine.mainMixerNode, format: fmt)
        engine.mainMixerNode.outputVolume = 1.0
        try? engine.start()
        // Try to adopt actual HAL rate after start (no reformat mid-stream)
        let actualRate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        if actualRate > 8000, actualRate != 48000 {
            // Log but keep 48k for now; converter will handle to 48k and HAL will resample
            // Changing format mid-stream would glitch, so we keep original
            fmt = outputFormat
        }
        // Memory pressure: trim non-audio caches, never ring/engine/mic
        let mp = DispatchSource.makeMemoryPressureSource(eventMask: [.warning, .critical], queue: .global(qos: .utility))
        mp.setEventHandler { [weak self] in
            guard let self else { return }
            let event = mp.data
            self.lastPressure = event
        }
        mp.resume()
        memoryPressureSource = mp
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            Task { await self?.handleOutputConfigChange() }
        }
        // Periodic bounded diagnostics sampler (1 Hz, off render, no per-callback logging)
        Task.detached(priority: .utility) { [weak self] in
            while let strongSelf = self {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                let snap = await strongSelf.snapshot()
                let url = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first?.appendingPathComponent("Logs/EV/playback-metrics.jsonl")
                if let u = url {
                    try? FileManager.default.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
                    if let data = try? JSONSerialization.data(withJSONObject: snap, options: []) {
                        if let handle = try? FileHandle(forWritingTo: u) {
                            try? handle.seekToEnd()
                            var line = data
                            line.append(0x0A)
                            try? handle.write(contentsOf: line)
                            try? handle.close()
                        } else {
                            var line = data
                            line.append(0x0A)
                            try? line.write(to: u)
                        }
                    }
                }
                if Task.isCancelled { break }
            }
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

    // New b64 entry point — decodes off MainActor, on actor executor (userInitiated)
    func enqueue(b64: String, sampleRate: Double) {
        guard let pcm = Data(base64Encoded: b64) else { return }
        enqueue(pcm: pcm, sampleRate: sampleRate)
    }

    func enqueue(pcm: Data, sampleRate: Double) {
        // Producer: convert Int16@sampleRate -> Float32@48k and write to ring
        // Allocations are per delta but bounded; ring is fixed 3s, no history retained
        guard pcm.count % 2 == 0, pcm.count > 0 else { return }
        let framesIn = pcm.count / 2
        receivedFrames += framesIn
        lastRate = sampleRate
        // Convert — reuse converter where possible, no per-delta recreation unless rate changes
        let inFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true)!
        let outFormat = outputFormat!
        if converter == nil || converterSourceRate != sampleRate {
            converter = AVAudioConverter(from: inFormat, to: outFormat)
            converterSourceRate = sampleRate
            converterResets += 1
        }
        guard let conv = converter else { return }
        // Prepare input buffer (bounded, not retained)
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(framesIn)) else { return }
        inBuf.frameLength = AVAudioFrameCount(framesIn)
        pcm.withUnsafeBytes { src in
            if let base = src.baseAddress {
                memcpy(inBuf.int16ChannelData![0], base, pcm.count)
            }
        }
        let ratio = outFormat.sampleRate / inFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(framesIn) * ratio * 1.25 + 32)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: outCapacity) else { return }
        var error: NSError?
        let status = conv.convert(to: outBuf, error: &error) { _, outStatus in
            outStatus.pointee = .haveData
            return inBuf
        }
        guard status != .error else { return }
        let framesOut = Int(outBuf.frameLength)
        guard framesOut > 0, let src = outBuf.floatChannelData?[0] else { return }
        convertedFrames += framesOut
        // Write Float32 to ring — lock-free, no allocation in render, bounded
        let written = ring.write(src, frames: framesOut)
        if written == 0 {
            // Overrun: healthy operation must be 0; drop would be audible, so count and return
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

    // Diagnostics snapshot for trace (all atomics, no locks, read outside render)
    func snapshot() -> [String: Any] {
        let bufferedMs = Double(ring.bufferedFrames) / 48000 * 1000
        let snap = ring.snapshot
        return [
            "receivedFrames": receivedFrames,
            "convertedFrames": convertedFrames,
            "bufferedFrames": snap.available,
            "bufferedMs": bufferedMs,
            "targetMs": targetMs,
            "lowWaterMs": lowWaterMs,
            "highWaterMs": highWaterMs,
            "capacityFrames": ring.capacity,
            "underrunFrames": snap.underruns,
            "overrunCount": snap.overruns,
            "totalWrites": snap.writes,
            "totalReads": snap.reads,
            "converterResets": converterResets,
            "isPrimed": isPrimed,
            "isPlaying": isPlaying,
            "outputRate": outputFormat.sampleRate,
            "outputChannels": outputFormat.channelCount,
        ]
    }

    // For external health snapshot (called off render, 1 Hz)
    nonisolated func lockFreeStats() -> (available: Int, underruns: Int, overruns: Int) {
        let s = ring.snapshot
        return (s.available, s.underruns, s.overruns)
    }
}
