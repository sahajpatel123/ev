import AVFoundation
import Atomics
import Foundation

/// True continuous playback: network producer → ring → hardware-clocked SourceNode.
/// All hot-path PCM flows off MainActor on this actor's executor.
/// Render callback is true lock-free SPSC: no mutex, no trylock, no allocation.
actor PlaybackCoordinator {
    static let shared = PlaybackCoordinator()
    nonisolated(unsafe) static var isPlayingSync = false
    // Half-duplex gate: while true, mic forwarding must be closed.
    // Updated only from PlaybackCoordinator actor, read synchronously from mic tap (real-time).
    nonisolated(unsafe) static var gateUntilNanos: UInt64 = 0
    nonisolated(unsafe) static var echoTailNanos: UInt64 = 250_000_000 // 250ms tail

    nonisolated static func shouldGateMic() -> Bool {
        if isPlayingSync { return true }
        let now = DispatchTime.now().uptimeNanoseconds
        return now < gateUntilNanos
    }

    // MARK: Ring buffer — true lock-free SPSC via swift-atomics (macOS 14)
    // Producer owns writeIndex, consumer owns readIndex. All atomics, no locks.
    private final class RingBuffer: @unchecked Sendable {
        let capacity: Int
        let ptr: UnsafeMutablePointer<Float>
        let writeIndex = ManagedAtomic<Int>(0)
        let readIndex = ManagedAtomic<Int>(0)
        let underrunFrames = ManagedAtomic<Int>(0)
        let overrunCount = ManagedAtomic<Int>(0)
        let totalWrites = ManagedAtomic<Int>(0)
        let totalReads = ManagedAtomic<Int>(0)

        init(capacityFrames: Int) {
            capacity = capacityFrames
            ptr = .allocate(capacity: capacity)
            ptr.initialize(repeating: 0, count: capacity)
        }
        deinit { ptr.deallocate() }

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
            writeIndex.store(w + frames, ordering: .releasing)
            totalWrites.wrappingIncrement(by: frames, ordering: .relaxed)
            return frames
        }

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
                underrunFrames.wrappingIncrement(by: frames - toCopy, ordering: .relaxed)
            }
            return toCopy
        }

        func clear() {
            readIndex.store(0, ordering: .releasing)
            writeIndex.store(0, ordering: .releasing)
        }

        var bufferedFrames: Int {
            let w = writeIndex.load(ordering: .acquiring)
            let r = readIndex.load(ordering: .acquiring)
            return w - r
        }

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
    private var memoryPressureSource: DispatchSourceMemoryPressure? = nil
    private var lastPressure: DispatchSource.MemoryPressureEvent = .normal

    init() {
        let capacityFrames = 48000 * 3
        let ringBuffer = RingBuffer(capacityFrames: capacityFrames)
        ring = ringBuffer
        var fmt = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
        outputFormat = fmt
        sourceNode = AVAudioSourceNode(format: fmt) { [ringBuffer] _, _, frameCount, audioBufferList -> OSStatus in
            let ablPointer = UnsafeMutableAudioBufferListPointer(audioBufferList)
            guard let buf = ablPointer.first, let dst = buf.mData?.assumingMemoryBound(to: Float.self) else { return noErr }
            let ring = ringBuffer
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
        let actualRate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        if actualRate > 8000, actualRate != 48000 {
            fmt = outputFormat
        }
        let mp = DispatchSource.makeMemoryPressureSource(eventMask: [.warning, .critical], queue: .global(qos: .utility))
        mp.setEventHandler { [weak self] in
            let event = mp.data
            Task { await self?.updatePressure(event) }
        }
        mp.resume()
        memoryPressureSource = mp
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            Task { await self?.handleOutputConfigChange() }
        }
        // Periodic bounded diagnostics sampler (1 Hz)
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
        // Gate monitor: poll ring depth every 15ms, update isPlayingSync + tail
        Task.detached(priority: .utility) { [weak self] in
            while let strongSelf = self {
                try? await Task.sleep(nanoseconds: 15_000_000)
                let buffered = await strongSelf.ringBuffered()
                let now = DispatchTime.now().uptimeNanoseconds
                if buffered > 0 {
                    PlaybackCoordinator.isPlayingSync = true
                } else if PlaybackCoordinator.isPlayingSync {
                    // Just drained
                    PlaybackCoordinator.isPlayingSync = false
                    PlaybackCoordinator.gateUntilNanos = now + PlaybackCoordinator.echoTailNanos
                }
                if Task.isCancelled { break }
            }
        }
    }

    private func ringBuffered() -> Int { ring.bufferedFrames }

    private func handleOutputConfigChange() {
        if _isPlaying { return }
        if !engine.isRunning {
            try? engine.start()
        }
    }

    private func updatePressure(_ event: DispatchSource.MemoryPressureEvent) {
        lastPressure = event
    }

    // MARK: Public API

    func setPlayer(_ player: TTSPlayer) {}

    func enqueue(b64: String, sampleRate: Double) {
        guard let pcm = Data(base64Encoded: b64) else { return }
        enqueue(pcm: pcm, sampleRate: sampleRate)
    }

    func enqueue(pcm: Data, sampleRate: Double) {
        guard pcm.count % 2 == 0, pcm.count > 0 else { return }
        let framesIn = pcm.count / 2
        receivedFrames += framesIn
        lastRate = sampleRate
        let inFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true)!
        let outFormat = outputFormat!
        if converter == nil || converterSourceRate != sampleRate {
            converter = AVAudioConverter(from: inFormat, to: outFormat)
            converterSourceRate = sampleRate
            converterResets += 1
        }
        guard let conv = converter else { return }
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
        let written = ring.write(src, frames: framesOut)
        if written == 0 { return }
        // Gate immediately on any successful write — half-duplex safety before first audible frame
        Self.isPlayingSync = true
        if !isPrimed {
            let bufferedMs = Double(ring.bufferedFrames) / 48000 * 1000
            if bufferedMs >= targetMs {
                isPrimed = true
                _isPlaying = true
                if !engine.isRunning {
                    try? engine.start()
                }
            } else if ring.bufferedFrames > 0 {
                // Short response: ensure engine is running even before prime
                if !engine.isRunning { try? engine.start() }
            }
        }
    }

    func stop() {
        ring.clear()
        isPrimed = false
        _isPlaying = false
        Self.isPlayingSync = false
        Self.gateUntilNanos = DispatchTime.now().uptimeNanoseconds + Self.echoTailNanos
        streamGeneration += 1
    }

    var isPlaying: Bool { _isPlaying && ring.bufferedFrames > 0 }
    var pendingFrames: Int { ring.bufferedFrames }

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
            "isPlayingSync": Self.isPlayingSync,
            "gateUntilNanos": Self.gateUntilNanos,
            "outputRate": outputFormat.sampleRate,
            "outputChannels": outputFormat.channelCount,
        ]
    }

    func lockFreeStats() -> (available: Int, underruns: Int, overruns: Int) {
        let s = ring.snapshot
        return (s.available, s.underruns, s.overruns)
    }
}
