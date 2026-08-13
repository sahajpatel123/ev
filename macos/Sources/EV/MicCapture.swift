import AVFoundation
import Foundation

/// Real AVFoundation microphone capture.
///
/// Requests TCC microphone permission, configures the capture session to
/// deliver 16 kHz mono 16-bit PCM (the EV voice API's wire format), and
/// accumulates the raw bytes until ``stop()`` returns them as base64-ready
/// Data.
final class MicCapture: NSObject {
    private let session = AVCaptureSession()
    private let output = AVCaptureAudioDataOutput()
    private var audioData = Data()
    private var isConfigured = false
    private let queue = DispatchQueue(label: "ev.mic.capture")

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
        guard configureIfNeeded() else { return false }
        audioData.removeAll(keepingCapacity: true)
        session.startRunning()
        return session.isRunning
    }

    func stop() -> Data? {
        if session.isRunning {
            session.stopRunning()
        }
        let captured = audioData
        audioData.removeAll(keepingCapacity: true)
        return captured.isEmpty ? nil : captured
    }

    private func configureIfNeeded() -> Bool {
        if isConfigured { return true }
        guard let device = AVCaptureDevice.default(for: .audio),
              let input = try? AVCaptureDeviceInput(device: device) else {
            return false
        }
        guard session.canAddInput(input), session.canAddOutput(output) else {
            return false
        }
        session.beginConfiguration()
        session.addInput(input)
        output.audioSettings = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        output.setSampleBufferDelegate(self, queue: queue)
        session.addOutput(output)
        session.commitConfiguration()
        isConfigured = true
        return true
    }
}

extension MicCapture: AVCaptureAudioDataOutputSampleBufferDelegate {
    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard let dataBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        var length = 0
        var pointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(
            dataBuffer,
            atOffset: 0,
            lengthAtOffsetOut: nil,
            totalLengthOut: &length,
            dataPointerOut: &pointer
        )
        guard status == kCMBlockBufferNoErr, let pointer, length > 0 else { return }
        audioData.append(Data(bytes: pointer, count: length))
    }
}
