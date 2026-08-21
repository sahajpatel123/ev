#if os(iOS) || os(macOS)
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

/// Authoritative camera owner for EV look / observe.
///
/// Capture runs on a dedicated queue, never the live audio graph. The session
/// is reused across a short idle window so sequential looks stay fast, then
/// released so the camera is not held forever.
public final class CameraManager: @unchecked Sendable {
    public static let shared = CameraManager()

    public struct Frame: Sendable {
        public let jpeg: Data
        public let width: Int
        public let height: Int
        public let cameraName: String
        public let permission: String
    }

    public enum CaptureError: Error, LocalizedError {
        case denied
        case unavailable
        case failed(String)
        case cancelled

        public var errorDescription: String? {
            switch self {
            case .denied:
                return "I can't access the camera because macOS hasn't granted EV camera access."
            case .unavailable:
                return "No camera is available on this device."
            case .failed(let message):
                return message
            case .cancelled:
                return "Camera observation stopped."
            }
        }

        public var code: String {
            switch self {
            case .denied: return "denied"
            case .unavailable: return "unavailable"
            case .failed: return "capture_failed"
            case .cancelled: return "cancelled"
            }
        }
    }

    private let captureQueue = DispatchQueue(label: "com.ev.camera.capture")
    private let gateQueue = DispatchQueue(label: "com.ev.camera.gate")
    private var session: AVCaptureSession?
    private var output: AVCapturePhotoOutput?
    private var currentDevice: AVCaptureDevice?
    private var idleStop: DispatchWorkItem?
    private var observeTask: Task<Void, Never>?
    private var captureChain: Task<Frame, Error>?

    private init() {}

    public func permissionState() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            return "authorized"
        case .denied, .restricted:
            return "denied"
        case .notDetermined:
            return "not_determined"
        @unknown default:
            return "unknown"
        }
    }

    public func requestAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            return true
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .video)
        default:
            return false
        }
    }

    public func captureJPEG() async throws -> Data {
        try await captureFrame().jpeg
    }

    public func captureFrame() async throws -> Frame {
        let next: Task<Frame, Error> = gateQueue.sync {
            let previous = captureChain
            let chained = Task<Frame, Error> {
                _ = try? await previous?.value
                return try await self.performCapture()
            }
            captureChain = chained
            return chained
        }
        return try await next.value
    }

    public func observe(
        duration: TimeInterval,
        interval: TimeInterval,
        maxFrames: Int,
        onFrame: @escaping @Sendable (Result<Frame, Error>, Int, Bool) -> Void
    ) {
        cancelObserve()
        let boundedDuration = min(max(duration, 1), 8)
        let boundedInterval = min(max(interval, 0.6), 3)
        let boundedMax = min(max(maxFrames, 1), 5)
        observeTask = Task.detached(priority: .userInitiated) { [weak self] in
            guard let self else { return }
            let deadline = Date().addingTimeInterval(boundedDuration)
            var index = 0
            while !Task.isCancelled, Date() < deadline, index < boundedMax {
                let last = index + 1 >= boundedMax || Date().addingTimeInterval(boundedInterval) >= deadline
                do {
                    let frame = try await self.captureFrame()
                    onFrame(.success(frame), index, last)
                } catch {
                    onFrame(.failure(error), index, true)
                    return
                }
                index += 1
                if last || Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: UInt64(boundedInterval * 1_000_000_000))
            }
        }
    }

    public func cancelObserve() {
        observeTask?.cancel()
        observeTask = nil
    }

    public func release() {
        cancelObserve()
        captureQueue.async { [weak self] in
            self?.stopSessionLocked()
        }
    }

    private func performCapture() async throws -> Frame {
        guard await requestAccess() else { throw CaptureError.denied }
        idleStop?.cancel()
        idleStop = nil
        return try await withCheckedThrowingContinuation { continuation in
            captureQueue.async {
                do {
                    let frame = try self.captureOnQueue()
                    continuation.resume(returning: frame)
                    self.scheduleIdleStop()
                } catch {
                    continuation.resume(throwing: error)
                    self.scheduleIdleStop()
                }
            }
        }
    }

    private func captureOnQueue() throws -> Frame {
        let device = try selectedDevice()
        let session = try preparedSession(device: device)
        if !session.isRunning {
            session.startRunning()
        }
        guard session.isRunning else {
            throw CaptureError.failed("Camera session failed to start.")
        }
        settleExposure(device)
        guard let output else {
            throw CaptureError.failed("Photo output missing.")
        }
        let jpeg = try capturePhoto(output: output)
        let sized = Self.constrainJPEG(jpeg)
        let dims = Self.jpegDimensions(sized) ?? (1280, 720)
        return Frame(
            jpeg: sized,
            width: dims.0,
            height: dims.1,
            cameraName: device.localizedName,
            permission: permissionState()
        )
    }

    private func selectedDevice() throws -> AVCaptureDevice {
        if let currentDevice { return currentDevice }
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [
                .builtInWideAngleCamera,
            ],
            mediaType: .video,
            position: .unspecified
        )
        let devices = discovery.devices
        let builtin = devices.first {
            let name = $0.localizedName.lowercased()
            return name.contains("facetime") || name.contains("built-in") || name.contains("studio")
        }
        guard let device = builtin
            ?? AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .unspecified)
            ?? AVCaptureDevice.default(for: .video)
            ?? devices.first
        else {
            throw CaptureError.unavailable
        }
        currentDevice = device
        return device
    }

    private func preparedSession(device: AVCaptureDevice) throws -> AVCaptureSession {
        if let session, output != nil, currentDevice?.uniqueID == device.uniqueID {
            return session
        }
        stopSessionLocked()
        let session = AVCaptureSession()
        session.beginConfiguration()
        if session.canSetSessionPreset(.hd1280x720) {
            session.sessionPreset = .hd1280x720
        } else if session.canSetSessionPreset(.high) {
            session.sessionPreset = .high
        }
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            throw CaptureError.failed("Could not add camera input.")
        }
        session.addInput(input)
        let output = AVCapturePhotoOutput()
        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            throw CaptureError.failed("Could not add photo output.")
        }
        session.addOutput(output)
        if let connection = output.connection(with: .video) {
            if connection.isVideoMirroringSupported {
                connection.automaticallyAdjustsVideoMirroring = false
                connection.isVideoMirrored = false
            }
        }
        session.commitConfiguration()
        self.session = session
        self.output = output
        self.currentDevice = device
        return session
    }

    private func settleExposure(_ device: AVCaptureDevice) {
        let deadline = Date().addingTimeInterval(0.35)
        while Date() < deadline {
            if !device.isAdjustingExposure && !device.isAdjustingWhiteBalance {
                break
            }
            Thread.sleep(forTimeInterval: 0.02)
        }
        if Date() < deadline {
            Thread.sleep(forTimeInterval: 0.08)
        }
    }

    private func capturePhoto(output: AVCapturePhotoOutput) throws -> Data {
        let settings = AVCapturePhotoSettings()
        let sink = PhotoSink()
        let semaphore = DispatchSemaphore(value: 0)
        sink.finish = { data, error in
            sink.result = (data, error)
            semaphore.signal()
        }
        withExtendedLifetime(sink) {
            output.capturePhoto(with: settings, delegate: sink)
            let wait = semaphore.wait(timeout: .now() + 6)
            if wait == .timedOut {
                sink.result = (nil, CaptureError.failed("Camera capture timed out."))
            }
        }
        if let error = sink.result?.1 {
            throw CaptureError.failed(error.localizedDescription)
        }
        guard let data = sink.result?.0, !data.isEmpty else {
            throw CaptureError.failed("Camera returned no photo data.")
        }
        return data
    }

    private func scheduleIdleStop() {
        idleStop?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.stopSessionLocked()
        }
        idleStop = work
        captureQueue.asyncAfter(deadline: .now() + 1.2, execute: work)
    }

    private func stopSessionLocked() {
        idleStop?.cancel()
        idleStop = nil
        session?.stopRunning()
        session?.inputs.forEach { session?.removeInput($0) }
        session?.outputs.forEach { session?.removeOutput($0) }
        session = nil
        output = nil
        currentDevice = nil
    }

    private static func constrainJPEG(_ data: Data, maxLongEdge: CGFloat = 1280, quality: CGFloat = 0.72) -> Data {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return data }
        let width = CGFloat(image.width)
        let height = CGFloat(image.height)
        let longest = max(width, height)
        let scale = longest > maxLongEdge ? maxLongEdge / longest : 1
        let targetW = max(1, Int(width * scale))
        let targetH = max(1, Int(height * scale))
        if scale == 1, data.count < 700_000 {
            return data
        }
        let color = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(
            data: nil,
            width: targetW,
            height: targetH,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: color,
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else { return data }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: targetW, height: targetH))
        guard let scaled = ctx.makeImage() else { return data }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(
            out, UTType.jpeg.identifier as CFString, 1, nil
        ) else { return data }
        CGImageDestinationAddImage(dest, scaled, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        CGImageDestinationFinalize(dest)
        return out as Data
    }

    private static func jpegDimensions(_ data: Data) -> (Int, Int)? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = props[kCGImagePropertyPixelWidth] as? Int,
              let height = props[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        return (width, height)
    }
}

private final class PhotoSink: NSObject, AVCapturePhotoCaptureDelegate {
    var finish: ((Data?, Error?) -> Void)?
    var result: (Data?, Error?)?
    private var finished = false

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        guard !finished else { return }
        finished = true
        finish?(photo.fileDataRepresentation(), error)
    }
}

public enum CameraFrameCapture {
    public typealias CaptureError = CameraManager.CaptureError

    public static func requestAccess() async -> Bool {
        await CameraManager.shared.requestAccess()
    }

    public static func captureJPEG() async throws -> Data {
        try await CameraManager.shared.captureJPEG()
    }
}
#endif
