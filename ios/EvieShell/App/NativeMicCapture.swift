import Foundation

#if os(iOS)
import AVFoundation

/// iPhone-native microphone capture for the EvieShell web core.
///
/// Field forensics (2026-10-08): across PWA builds 2026.10.07.2, .08.1 and
/// .08.2 the phone's tap emitted exactly one beacon (`tap_entry`) before the
/// web capture stack died — no `tap_gum_*`, hang, watchdog or incident ever
/// reached the server. The WKWebView getUserMedia path is the failure point;
/// this engine replaces it on iPhone. The PWA keeps its web capture path for
/// non-shell browsers.
///
/// Contract (all requests bounded, all replies JSON-serializable):
///   mic_start -> {ok, sample_rate, frame_ms, engine} | {ok:false, failure}
///   mic_read  -> {ok, pcm_b64, samples, sample_rate} | {ok:true, idle:true}
///              | {ok:false, failure}
///   mic_stop  -> {ok}
@MainActor
final class NativeMicCapture {
    static let shared = NativeMicCapture()

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var running = false
    private var generation = 0
    private var pending = Data()
    private var chunks: [Data] = []
    private let sampleRate: Double = 16_000
    private let chunkSamples = 1_600  // 100 ms @ 16 kHz mono Int16
    private let maxQueuedChunks = 25  // ~2.5 s; JS pulls at 10 reads/s

    // MARK: - Bridge contract

    func start() async -> [String: Any] {
        if running {
            return ["ok": true, "sample_rate": sampleRate, "frame_ms": 100, "already": true]
        }
        guard await Self.requestMicrophoneAccess() else {
            return ["ok": false, "failure": "PERMISSION_DENIED"]
        }
        do {
            let session = AVAudioSession.sharedInstance()
            // playAndRecord so TTS playback in the webview keeps working;
            // mixWithOthers avoids ducking or interrupting the PWA engine.
            try session.setCategory(
                .playAndRecord,
                mode: .default,
                options: [.defaultToSpeaker, .allowBluetooth, .mixWithOthers]
            )
            try session.setActive(true)
        } catch {
            return ["ok": false, "failure": "AUDIO_SESSION", "detail": String(describing: error)]
        }
        let input = engine.inputNode
        let hardwareFormat = input.inputFormat(forBus: 0)
        guard hardwareFormat.sampleRate > 0, hardwareFormat.channelCount > 0 else {
            return ["ok": false, "failure": "NO_INPUT", "detail": "input format unavailable"]
        }
        guard
            let target = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: true
            ),
            let converter = AVAudioConverter(from: hardwareFormat, to: target)
        else {
            return ["ok": false, "failure": "CONVERTER"]
        }
        self.converter = converter
        pending.removeAll(keepingCapacity: true)
        chunks.removeAll(keepingCapacity: true)
        generation += 1
        let currentGeneration = generation
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: hardwareFormat) { [weak self] buffer, _ in
            guard let self else { return }
            let data = Self.convert(buffer, using: converter, to: target)
            guard let data, !data.isEmpty else { return }
            Task { @MainActor in
                self.publish(data, generation: currentGeneration)
            }
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            self.converter = nil
            return ["ok": false, "failure": "ENGINE_START", "detail": String(describing: error)]
        }
        running = true
        return ["ok": true, "sample_rate": sampleRate, "frame_ms": 100, "engine": "avaudioengine"]
    }

    func read(timeoutMs: Int) async -> [String: Any] {
        guard running else { return ["ok": false, "failure": "MIC_NOT_RUNNING"] }
        let deadline = Date().addingTimeInterval(Double(max(50, timeoutMs)) / 1000.0)
        while running {
            if !chunks.isEmpty {
                let data = chunks.removeFirst()
                return [
                    "ok": true,
                    "pcm_b64": data.base64EncodedString(),
                    "samples": data.count / MemoryLayout<Int16>.size,
                    "sample_rate": sampleRate,
                ]
            }
            if Date() >= deadline {
                return ["ok": true, "pcm_b64": NSNull(), "idle": true]
            }
            try? await Task.sleep(nanoseconds: 10_000_000)  // 10 ms
        }
        return ["ok": false, "failure": "MIC_NOT_RUNNING"]
    }

    func stop() -> [String: Any] {
        generation += 1
        if engine.isRunning {
            engine.stop()
        }
        engine.inputNode.removeTap(onBus: 0)
        converter = nil
        running = false
        pending.removeAll(keepingCapacity: true)
        chunks.removeAll(keepingCapacity: true)
        return ["ok": true]
    }

    // MARK: - Internals

    private func publish(_ data: Data, generation: Int) {
        guard running, generation == self.generation else { return }
        pending.append(data)
        let chunkBytes = chunkSamples * MemoryLayout<Int16>.size
        while pending.count >= chunkBytes {
            let chunk = pending.prefix(chunkBytes)
            pending.removeFirst(chunkBytes)
            chunks.append(Data(chunk))
        }
        while chunks.count > maxQueuedChunks {
            chunks.removeFirst()
        }
    }

    private static func requestMicrophoneAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .denied, .restricted:
            return false
        default:
            return await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    private static func convert(
        _ buffer: AVAudioPCMBuffer,
        using converter: AVAudioConverter,
        to target: AVAudioFormat
    ) -> Data? {
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            return nil
        }
        var provided = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, outStatus in
            if provided {
                outStatus.pointee = .noDataNow
                return nil
            }
            provided = true
            outStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, output.frameLength > 0, let samples = output.int16ChannelData?[0] else {
            return nil
        }
        return Data(
            bytes: samples,
            count: Int(output.frameLength) * MemoryLayout<Int16>.size
        )
    }
}

#else

/// Non-iOS builds (macOS broker checks) report the capability as unavailable.
final class NativeMicCapture {
    static let shared = NativeMicCapture()
    func start() async -> [String: Any] { ["ok": false, "failure": "UNSUPPORTED"] }
    func read(timeoutMs: Int) async -> [String: Any] { ["ok": false, "failure": "UNSUPPORTED"] }
    func stop() -> [String: Any] { ["ok": false, "failure": "UNSUPPORTED"] }
}

#endif
