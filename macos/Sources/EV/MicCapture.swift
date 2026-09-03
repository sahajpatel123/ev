import AVFoundation
import EVRuntime
import Foundation

/// Real AVAudioEngine microphone capture.
///
/// AVCaptureAudioDataOutput routinely ignores the 16 kHz PCM16 `audioSettings`
/// we used to set, and delivers 48 kHz float32 instead. Wrapping those bytes
/// as a 16 kHz int16 WAV made Whisper treat a 10 s hold as ~60 s of noise —
/// long enough for the menu-bar client to time out on "thinking".
///
/// This capture converts the hardware stream to 16 kHz mono 16-bit PCM and
/// keeps the clip bounded for memory safety while allowing normal long-form
/// questions and dictation.
final class MicCapture: NSObject {
    static let sampleRate: Double = 16_000
    static let maxSeconds: Double = 120

    private var engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var pcm = Data()
    private let lock = NSLock()
    private var tapInstalled = false

    func requestPermission() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            return await AppForeground.withActivation {
                await AVCaptureDevice.requestAccess(for: .audio)
            }
        default:
            return false
        }
    }

    func start() async -> Bool {
        guard await requestPermission() else { return false }
        stopEngine()
        guard AudioInputLease.acquire(.clip) else { return false }
        pcm.removeAll(keepingCapacity: true)

        // Recreate after a just-accepted grant — an engine allocated
        // before Allow reports 0 Hz / 0 ch and installTap aborts.
        engine = AVAudioEngine()
        let hwFormat: AVAudioFormat
        do {
            hwFormat = try ObjCException.attachAndPrepare(engine)
        } catch {
            AudioInputLease.release(.clip)
            return false
        }
        let input = engine.inputNode
        guard hwFormat.sampleRate > 0, hwFormat.channelCount > 0 else {
            AudioInputLease.release(.clip)
            return false
        }
        guard let destFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Self.sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            AudioInputLease.release(.clip)
            return false
        }
        converter = AVAudioConverter(from: hwFormat, to: destFormat)
        if converter == nil {
            let alreadyDest = hwFormat.commonFormat == .pcmFormatInt16
                && abs(hwFormat.sampleRate - Self.sampleRate) < 0.5
                && hwFormat.channelCount == 1
            if !alreadyDest {
                AudioInputLease.release(.clip)
                return false
            }
        }
        do {
            try ObjCException.installTap(on: input, bufferSize: 2048, format: hwFormat) { [weak self] buffer, _ in
                self?.append(buffer)
            }
        } catch {
            AudioInputLease.release(.clip)
            return false
        }
        tapInstalled = true
        do {
            try ObjCException.start(engine)
            return true
        } catch {
            stopEngine()
            AudioInputLease.release(.clip)
            return false
        }
    }

    func stop() -> Data? {
        stopEngine()
        AudioInputLease.release(.clip)
        lock.lock()
        let captured = pcm
        pcm.removeAll(keepingCapacity: true)
        lock.unlock()
        guard !captured.isEmpty else { return nil }
        return Self.pcm16Wave(captured)
    }

    /// Wrap 16 kHz mono PCM16 as a RIFF/WAVE payload the voice API accepts.
    static func pcm16Wave(_ pcm: Data, sampleRate: Int = 16_000) -> Data {
        var wav = Data()
        let dataSize = UInt32(pcm.count)
        let fileSize = 36 + dataSize
        func appendUInt32(_ value: UInt32) {
            var le = value.littleEndian
            wav.append(Data(bytes: &le, count: 4))
        }
        func appendUInt16(_ value: UInt16) {
            var le = value.littleEndian
            wav.append(Data(bytes: &le, count: 2))
        }
        wav.append(contentsOf: Array("RIFF".utf8))
        appendUInt32(fileSize)
        wav.append(contentsOf: Array("WAVEfmt ".utf8))
        appendUInt32(16)
        appendUInt16(1)
        appendUInt16(1)
        appendUInt32(UInt32(sampleRate))
        appendUInt32(UInt32(sampleRate * 2))
        appendUInt16(2)
        appendUInt16(16)
        wav.append(contentsOf: Array("data".utf8))
        appendUInt32(dataSize)
        wav.append(pcm)
        return wav
    }

    static func durationSeconds(_ wav: Data, sampleRate: Double = sampleRate) -> Double {
        let pcmBytes = max(0, wav.count - 44)
        return Double(pcmBytes) / 2.0 / sampleRate
    }

    static func isQuiet(_ wav: Data, rmsThreshold: Double = 80) -> Bool {
        let pcm = wav.count > 44 ? wav.dropFirst(44) : wav[...]
        guard pcm.count >= 2 else { return true }
        let count = pcm.count / 2
        var sumSquares: Double = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for index in 0..<count {
                let sample = Double(samples[index].littleEndian)
                sumSquares += sample * sample
            }
        }
        let rms = sqrt(sumSquares / Double(count))
        return rms < rmsThreshold
    }

    private func stopEngine() {
        if tapInstalled {
            ObjCException.removeTap(on: engine.inputNode)
            tapInstalled = false
        }
        ObjCException.stop(engine)
        converter = nil
    }

    private func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        let maxBytes = Int(Self.sampleRate * 2 * Self.maxSeconds)
        guard pcm.count < maxBytes else { return }
        let converted = convertToInt16Mono(buffer)
        guard !converted.isEmpty else { return }
        let remaining = maxBytes - pcm.count
        if converted.count > remaining {
            pcm.append(converted.prefix(remaining - remaining % 2))
        } else {
            pcm.append(converted)
        }
    }

    private func convertToInt16Mono(_ buffer: AVAudioPCMBuffer) -> Data {
        guard buffer.frameLength > 0 else { return Data() }
        if let converter {
            let ratio = converter.outputFormat.sampleRate / max(buffer.format.sampleRate, 1)
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 256
            guard let out = AVAudioPCMBuffer(
                pcmFormat: converter.outputFormat,
                frameCapacity: capacity
            ) else {
                return Data()
            }
            var error: NSError?
            var consumed = false
            let status = converter.convert(to: out, error: &error) { _, outStatus in
                if consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                consumed = true
                outStatus.pointee = .haveData
                return buffer
            }
            if status == .error { return Data() }
            return int16MonoData(out)
        }
        return int16MonoData(buffer)
    }

    private func int16MonoData(_ buffer: AVAudioPCMBuffer) -> Data {
        let frames = Int(buffer.frameLength)
        guard frames > 0 else { return Data() }
        if let channels = buffer.int16ChannelData {
            if buffer.format.isInterleaved || buffer.format.channelCount == 1 {
                return Data(bytes: channels[0], count: frames * MemoryLayout<Int16>.size)
            }
            var mixed = [Int16](repeating: 0, count: frames)
            let left = channels[0]
            if buffer.format.channelCount >= 2 {
                let right = channels[1]
                for index in 0..<frames {
                    mixed[index] = Int16((Int32(left[index]) + Int32(right[index])) / 2)
                }
            } else {
                mixed = Array(UnsafeBufferPointer(start: left, count: frames))
            }
            return mixed.withUnsafeBufferPointer { Data(buffer: $0) }
        }
        let abl = buffer.audioBufferList.pointee.mBuffers
        let bytes = Int(abl.mDataByteSize)
        guard let pointer = abl.mData, bytes > 0 else { return Data() }
        return Data(bytes: pointer, count: bytes)
    }
}
