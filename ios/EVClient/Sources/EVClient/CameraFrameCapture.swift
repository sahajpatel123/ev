#if os(iOS) || os(macOS)
import AVFoundation
import Foundation

/// Single-frame camera grab for a live `look`. There is no background stream:
/// the session starts, one JPEG is taken, then the session stops.
public enum CameraFrameCapture {
    public enum CaptureError: Error, LocalizedError {
        case denied
        case unavailable
        case failed(String)

        public var errorDescription: String? {
            switch self {
            case .denied:
                return "Camera permission denied. Grant camera access in Privacy settings."
            case .unavailable:
                return "No camera is available on this device."
            case .failed(let message):
                return message
            }
        }
    }

    public static func requestAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            return true
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .video)
        default:
            return false
        }
    }

    public static func captureJPEG() async throws -> Data {
        guard await requestAccess() else { throw CaptureError.denied }
        guard let device = AVCaptureDevice.default(for: .video) else {
            throw CaptureError.unavailable
        }
        let session = await MainActor.run { SessionBox(device: device) }
        return try await session.capture()
    }
}

@MainActor
private final class SessionBox {
    private let device: AVCaptureDevice
    private let session = AVCaptureSession()
    private let output = AVCapturePhotoOutput()
    private var delegate: PhotoSink?

    init(device: AVCaptureDevice) {
        self.device = device
    }

    func capture() async throws -> Data {
        let input: AVCaptureDeviceInput
        do {
            input = try AVCaptureDeviceInput(device: device)
        } catch {
            throw CameraFrameCapture.CaptureError.failed(error.localizedDescription)
        }
        session.beginConfiguration()
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            throw CameraFrameCapture.CaptureError.failed("Could not add camera input.")
        }
        session.addInput(input)
        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            throw CameraFrameCapture.CaptureError.failed("Could not add photo output.")
        }
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
        guard session.isRunning else {
            throw CameraFrameCapture.CaptureError.failed("Camera session failed to start.")
        }
        defer { session.stopRunning() }
        return try await withCheckedThrowingContinuation { continuation in
            let sink = PhotoSink { data, error in
                if let error {
                    continuation.resume(
                        throwing: CameraFrameCapture.CaptureError.failed(error.localizedDescription)
                    )
                } else if let data, !data.isEmpty {
                    continuation.resume(returning: data)
                } else {
                    continuation.resume(
                        throwing: CameraFrameCapture.CaptureError.failed("Camera returned no photo data.")
                    )
                }
            }
            self.delegate = sink
            self.output.capturePhoto(with: AVCapturePhotoSettings(), delegate: sink)
        }
    }
}

private final class PhotoSink: NSObject, AVCapturePhotoCaptureDelegate {
    private let finish: (Data?, Error?) -> Void
    private var finished = false

    init(finish: @escaping (Data?, Error?) -> Void) {
        self.finish = finish
    }

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        guard !finished else { return }
        finished = true
        finish(photo.fileDataRepresentation(), error)
    }
}
#endif
