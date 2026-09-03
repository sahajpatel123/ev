#if os(iOS) || os(macOS)
import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ImageIO
import Photos
import UniformTypeIdentifiers
import Vision

/// Authoritative camera owner for EV look, still capture, observe, and record.
///
/// Capture runs on a dedicated queue, never the live audio graph. The session
/// stays warm across a short idle window so sequential looks are not cold,
/// then releases so the camera is not held forever.
public final class CameraManager: @unchecked Sendable {
    public static let shared = CameraManager()

    public struct Frame: Sendable {
        public let jpeg: Data
        public let width: Int
        public let height: Int
        public let cameraName: String
        public let permission: String
        public let luminance: Double
        public let labels: [String]
        public let ocrText: String
        public let faceCount: Int
        public let personCount: Int
        public let lighting: String
        public let colors: [String]
    }

    public struct SavedPhoto: Sendable {
        public let path: String
        public let filename: String
        public let addedToPhotos: Bool
    }

    public struct Clip: Sendable {
        public let fileURL: URL
        public let savedPath: String
        public let duration: TimeInterval
        public let width: Int
        public let height: Int
        public let cameraName: String
        public let permission: String
        public let posterJPEGs: [Data]
        public let hasAudio: Bool
        public let addedToPhotos: Bool
        public let luminance: Double
        public let labels: [String]
        public let ocrText: String
        public let faceCount: Int
        public let personCount: Int
        public let lighting: String
        public let colors: [String]

        public var posterJPEG: Data? { posterJPEGs.first }
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

    private enum SessionMode {
        case photo
        case movie
    }

    private let captureQueue = DispatchQueue(label: "com.ev.camera.capture")
    private let gateQueue = DispatchQueue(label: "com.ev.camera.gate")
    private var serialWork: Task<Void, Never>?
    private var session: AVCaptureSession?
    private var output: AVCapturePhotoOutput?
    private var movieOutput: AVCaptureMovieFileOutput?
    private var currentDevice: AVCaptureDevice?
    private var audioInput: AVCaptureDeviceInput?
    private var idleStop: DispatchWorkItem?
    private var observeTask: Task<Void, Never>?
    private var movieSink: MovieSink?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var videoWriter: VideoWriterSink?
    private var sessionWasCold = true

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

    public func captureFrame(forSave: Bool = false) async throws -> Frame {
        try await enqueue {
            try await self.performCapture(forSave: forSave)
        }
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

    public func cancelRecording() {
        captureQueue.async { [weak self] in
            self?.videoWriter?.requestStop()
            self?.movieOutput?.stopRecording()
        }
    }

    public func recordClip(duration: TimeInterval) async throws -> Clip {
        cancelObserve()
        let bounded = min(max(duration, 2), 30)
        return try await enqueue {
            try await self.performRecord(duration: bounded)
        }
    }

    public func savePhoto(_ jpeg: Data) -> SavedPhoto {
        let filename = Self.timestampedName(prefix: "EV", ext: "jpg")
        let url = Self.mediaDirectory(kind: .photo).appendingPathComponent(filename)
        try? jpeg.write(to: url, options: .atomic)
        let added = Self.addToPhotos(fileURL: url, isVideo: false)
        return SavedPhoto(path: url.path, filename: filename, addedToPhotos: added)
    }

    public func release() {
        cancelObserve()
        captureQueue.async { [weak self] in
            self?.stopSessionLocked()
        }
    }

    private func enqueue<T: Sendable>(_ body: @escaping @Sendable () async throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            gateQueue.async {
                let previous = self.serialWork
                let work = Task {
                    await previous?.value
                    do {
                        let value = try await body()
                        continuation.resume(returning: value)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
                self.serialWork = Task { _ = await work.value }
            }
        }
    }

    private func performCapture(forSave: Bool) async throws -> Frame {
        guard await requestAccess() else { throw CaptureError.denied }
        idleStop?.cancel()
        idleStop = nil
        return try await withCheckedThrowingContinuation { continuation in
            captureQueue.async {
                do {
                    let frame = try self.captureOnQueue(forSave: forSave)
                    continuation.resume(returning: frame)
                    self.scheduleIdleStop()
                } catch {
                    continuation.resume(throwing: error)
                    self.scheduleIdleStop()
                }
            }
        }
    }

    private func captureOnQueue(forSave: Bool) throws -> Frame {
        let device = try selectedDevice()
        let session = try preparedSession(device: device, mode: .photo)
        if !session.isRunning {
            session.startRunning()
            sessionWasCold = true
        }
        guard session.isRunning else {
            throw CaptureError.failed("Camera session failed to start.")
        }
        configureExposure(device)
        settleExposure(device, seconds: sessionWasCold ? 1.15 : 0.35)
        sessionWasCold = false
        guard let output else {
            throw CaptureError.failed("Photo output missing.")
        }
        var jpeg = try capturePhoto(output: output)
        var analysis = Self.analyze(jpeg)
        if analysis.luminance < 0.12 {
            Self.boostExposureIfAvailable(device)
            settleExposure(device, seconds: 0.7)
            jpeg = try capturePhoto(output: output)
            analysis = Self.analyze(jpeg)
            Self.resetExposureBiasIfAvailable(device)
        }
        let maxEdge: CGFloat = forSave ? 1920 : 1280
        let quality: CGFloat = forSave ? 0.92 : 0.88
        let sized = Self.constrainJPEG(jpeg, maxLongEdge: maxEdge, quality: quality)
        let dims = Self.jpegDimensions(sized) ?? (1280, 720)
        let finalAnalysis = sized.count == jpeg.count ? analysis : Self.analyze(sized)
        return Frame(
            jpeg: sized,
            width: dims.0,
            height: dims.1,
            cameraName: device.localizedName,
            permission: permissionState(),
            luminance: finalAnalysis.luminance,
            labels: finalAnalysis.labels,
            ocrText: finalAnalysis.ocrText,
            faceCount: finalAnalysis.faceCount,
            personCount: finalAnalysis.personCount,
            lighting: finalAnalysis.lighting,
            colors: finalAnalysis.colors
        )
    }

    private func performRecord(duration: TimeInterval) async throws -> Clip {
        guard await requestAccess() else { throw CaptureError.denied }
        idleStop?.cancel()
        idleStop = nil
        let poster = try? await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Frame, Error>) in
            captureQueue.async {
                do {
                    continuation.resume(returning: try self.captureOnQueue(forSave: true))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("ev-\(UUID().uuidString).mov")
        do {
            try await recordWithWriter(duration: duration, to: url)
        } catch {
            try? FileManager.default.removeItem(at: url)
            do {
                try await recordWithMovieFile(duration: duration, to: url)
            } catch {
                scheduleIdleStop()
                throw error
            }
        }
        scheduleIdleStop()
        return try finishClip(at: url, requestedDuration: duration, posterHint: poster)
    }

    private func recordWithWriter(duration: TimeInterval, to url: URL) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            captureQueue.async {
                do {
                    try self.startWriterLocked(to: url)
                    continuation.resume()
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
        guard let sink = videoWriter else {
            throw CaptureError.failed("Camera recording did not start.")
        }
        let started = await sink.waitForStart(timeout: 2.0)
        if !started {
            await stopWriterAsyncIgnoringResult()
            throw CaptureError.failed("Camera recording did not start.")
        }
        do {
            try await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
        } catch {
            _ = try? await stopWriterAsync()
            throw CaptureError.cancelled
        }
        _ = try await stopWriterAsync()
    }

    private func startWriterLocked(to url: URL) throws {
        let device = try selectedDevice()
        let session = try preparedSession(device: device, mode: .movie)
        if !session.isRunning {
            session.startRunning()
            sessionWasCold = true
        }
        guard session.isRunning else {
            throw CaptureError.failed("Camera session failed to start.")
        }
        configureExposure(device)
        settleExposure(device, seconds: sessionWasCold ? 0.8 : 0.25)
        sessionWasCold = false
        guard let videoOutput else {
            throw CaptureError.failed("Movie output missing.")
        }
        try? FileManager.default.removeItem(at: url)
        let sink = VideoWriterSink(url: url)
        videoWriter = sink
        videoOutput.setSampleBufferDelegate(sink, queue: DispatchQueue(label: "com.ev.camera.video"))
    }

    private func stopWriterAsync() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            captureQueue.async {
                guard let sink = self.videoWriter else {
                    continuation.resume(throwing: CaptureError.failed("Recording was not active."))
                    return
                }
                sink.finish { result in
                    self.videoWriter = nil
                    self.videoOutput?.setSampleBufferDelegate(nil, queue: nil)
                    switch result {
                    case .success(let url):
                        continuation.resume(returning: url)
                    case .failure(let error):
                        continuation.resume(throwing: CaptureError.failed(error.localizedDescription))
                    }
                }
            }
        }
    }

    private func stopWriterAsyncIgnoringResult() async {
        _ = try? await stopWriterAsync()
    }

    private func recordWithMovieFile(duration: TimeInterval, to url: URL) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            captureQueue.async {
                do {
                    try self.startRecordingLocked(to: url)
                    continuation.resume()
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
        let started = await waitUntilMovieRecording(timeout: 2.0)
        if !started {
            _ = try? await stopRecordingAsync()
            throw CaptureError.failed("Camera recording did not start.")
        }
        do {
            try await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
        } catch {
            _ = try? await stopRecordingAsync()
            throw CaptureError.cancelled
        }
        _ = try await stopRecordingAsync()
    }

    private func waitUntilMovieRecording(timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let rolling: Bool = await withCheckedContinuation { continuation in
                captureQueue.async {
                    continuation.resume(returning: self.movieOutput?.isRecording == true || self.movieSink?.started == true)
                }
            }
            if rolling { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return false
    }

    private func startRecordingLocked(to url: URL) throws {
        let device = try selectedDevice()
        let session = try preparedSession(device: device, mode: .movie)
        if !session.isRunning {
            session.startRunning()
            sessionWasCold = true
        }
        guard session.isRunning else {
            throw CaptureError.failed("Camera session failed to start.")
        }
        configureExposure(device)
        settleExposure(device, seconds: sessionWasCold ? 0.8 : 0.25)
        sessionWasCold = false
        guard let movieOutput else {
            throw CaptureError.failed("Movie output missing.")
        }
        if movieOutput.isRecording {
            movieOutput.stopRecording()
        }
        try? FileManager.default.removeItem(at: url)
        let sink = MovieSink()
        movieSink = sink
        movieOutput.startRecording(to: url, recordingDelegate: sink)
    }

    private func stopRecordingAsync() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            captureQueue.async {
                guard let movieOutput = self.movieOutput, let sink = self.movieSink else {
                    continuation.resume(throwing: CaptureError.failed("Recording was not active."))
                    return
                }
                if movieOutput.isRecording {
                    sink.finish = { url, error in
                        if let error {
                            continuation.resume(throwing: CaptureError.failed(error.localizedDescription))
                        } else if let url {
                            continuation.resume(returning: url)
                        } else {
                            continuation.resume(throwing: CaptureError.failed("Recording produced no file."))
                        }
                    }
                    movieOutput.stopRecording()
                } else if let url = sink.finishedURL {
                    continuation.resume(returning: url)
                } else {
                    continuation.resume(throwing: CaptureError.failed("Recording produced no file."))
                }
            }
        }
    }

    private func finishClip(at url: URL, requestedDuration: TimeInterval, posterHint: Frame?) throws -> Clip {
        let asset = AVURLAsset(url: url)
        let duration = CMTimeGetSeconds(asset.duration)
        let keptDuration = duration > 0.2 ? duration : requestedDuration
        var width = 1280
        var height = 720
        if let track = asset.tracks(withMediaType: .video).first {
            let size = track.naturalSize.applying(track.preferredTransform)
            width = max(1, Int(abs(size.width)))
            height = max(1, Int(abs(size.height)))
        }
        var posters = Self.posterJPEGs(from: url)
        if posters.isEmpty, let hint = posterHint?.jpeg {
            posters = [hint]
        }
        if let first = posters.first, let dims = Self.jpegDimensions(first) {
            width = dims.0
            height = dims.1
        }
        let analyses = posters.map { Self.analyze($0) }
        let analysis: Analysis
        if !analyses.isEmpty {
            analysis = Self.mergeAnalyses(analyses)
        } else if let hint = posterHint {
            analysis = Analysis(
                luminance: hint.luminance,
                labels: hint.labels,
                ocrText: hint.ocrText,
                faceCount: hint.faceCount,
                personCount: hint.personCount,
                lighting: hint.lighting,
                colors: hint.colors
            )
        } else {
            analysis = Analysis(
                luminance: 0.5,
                labels: [],
                ocrText: "",
                faceCount: 0,
                personCount: 0,
                lighting: "normally lit",
                colors: []
            )
        }
        let filename = Self.timestampedName(prefix: "EV", ext: "mov")
        let savedURL = Self.mediaDirectory(kind: .video).appendingPathComponent(filename)
        try? FileManager.default.removeItem(at: savedURL)
        do {
            try FileManager.default.copyItem(at: url, to: savedURL)
        } catch {
            try? FileManager.default.moveItem(at: url, to: savedURL)
        }
        let added = Self.addToPhotos(fileURL: savedURL, isVideo: true)
        return Clip(
            fileURL: savedURL,
            savedPath: savedURL.path,
            duration: keptDuration,
            width: width,
            height: height,
            cameraName: currentDevice?.localizedName ?? posterHint?.cameraName ?? "Camera",
            permission: permissionState(),
            posterJPEGs: posters,
            hasAudio: !(asset.tracks(withMediaType: .audio).isEmpty),
            addedToPhotos: added,
            luminance: analysis.luminance,
            labels: analysis.labels,
            ocrText: analysis.ocrText,
            faceCount: analysis.faceCount,
            personCount: analysis.personCount,
            lighting: analysis.lighting,
            colors: analysis.colors
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

    private func preparedSession(device: AVCaptureDevice, mode: SessionMode) throws -> AVCaptureSession {
        if mode == .photo,
           let session,
           output != nil,
           currentDevice?.uniqueID == device.uniqueID {
            return session
        }
        if mode == .movie,
           let session,
           output == nil,
           (videoOutput != nil || movieOutput != nil),
           currentDevice?.uniqueID == device.uniqueID {
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
        if mode == .photo {
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
            self.output = output
        } else {
            let video = AVCaptureVideoDataOutput()
            video.alwaysDiscardsLateVideoFrames = true
            video.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
            ]
            if session.canAddOutput(video) {
                session.addOutput(video)
                videoOutput = video
            }
            let movie = AVCaptureMovieFileOutput()
            if session.canAddOutput(movie) {
                session.addOutput(movie)
                movieOutput = movie
            }
            if videoOutput == nil && movieOutput == nil {
                session.commitConfiguration()
                throw CaptureError.failed("Could not add movie output.")
            }
        }
        session.commitConfiguration()
        self.session = session
        self.currentDevice = device
        sessionWasCold = true
        return session
    }

    private func configureExposure(_ device: AVCaptureDevice) {
        guard device.isConnected else { return }
        do {
            try device.lockForConfiguration()
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
            if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
            #if os(iOS)
            if device.isLowLightBoostSupported {
                device.automaticallyEnablesLowLightBoostWhenAvailable = true
            }
            #endif
            device.unlockForConfiguration()
        } catch {
            return
        }
    }

    private static func boostExposureIfAvailable(_ device: AVCaptureDevice) {
        #if os(iOS)
        guard device.isExposureTargetBiasSupported else { return }
        do {
            try device.lockForConfiguration()
            let next = min(device.maxExposureTargetBias, max(0.45, device.exposureTargetBias + 0.55))
            device.setExposureTargetBias(next, completionHandler: nil)
            device.unlockForConfiguration()
        } catch {
            return
        }
        #else
        _ = device
        #endif
    }

    private static func resetExposureBiasIfAvailable(_ device: AVCaptureDevice) {
        #if os(iOS)
        guard device.isExposureTargetBiasSupported else { return }
        do {
            try device.lockForConfiguration()
            device.setExposureTargetBias(0, completionHandler: nil)
            device.unlockForConfiguration()
        } catch {
            return
        }
        #else
        _ = device
        #endif
    }

    private func settleExposure(_ device: AVCaptureDevice, seconds: TimeInterval) {
        let deadline = Date().addingTimeInterval(max(0.15, seconds))
        while Date() < deadline {
            if !device.isAdjustingExposure && !device.isAdjustingWhiteBalance && !device.isAdjustingFocus {
                Thread.sleep(forTimeInterval: 0.08)
                break
            }
            Thread.sleep(forTimeInterval: 0.03)
        }
        if Date() < deadline {
            Thread.sleep(forTimeInterval: min(0.12, deadline.timeIntervalSinceNow))
        }
    }

    private func capturePhoto(output: AVCapturePhotoOutput) throws -> Data {
        try captureWithSettings(output: output, settings: AVCapturePhotoSettings())
    }

    private func captureWithSettings(output: AVCapturePhotoOutput, settings: AVCapturePhotoSettings) throws -> Data {
        let sink = PhotoSink()
        let semaphore = DispatchSemaphore(value: 0)
        sink.finish = { data, error in
            sink.result = (data, error)
            semaphore.signal()
        }
        withExtendedLifetime(sink) {
            output.capturePhoto(with: settings, delegate: sink)
            let wait = semaphore.wait(timeout: .now() + 8)
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
        captureQueue.asyncAfter(deadline: .now() + 12, execute: work)
    }

    private func stopSessionLocked() {
        idleStop?.cancel()
        idleStop = nil
        if movieOutput?.isRecording == true {
            movieOutput?.stopRecording()
        }
        session?.stopRunning()
        session?.inputs.forEach { session?.removeInput($0) }
        session?.outputs.forEach { session?.removeOutput($0) }
        session = nil
        output = nil
        movieOutput = nil
        videoOutput = nil
        videoWriter?.requestStop()
        videoWriter = nil
        audioInput = nil
        currentDevice = nil
        movieSink = nil
        sessionWasCold = true
    }

    private enum MediaKind {
        case photo
        case video
    }

    private static func mediaDirectory(kind: MediaKind) -> URL {
        #if os(macOS)
        let search: FileManager.SearchPathDirectory = kind == .photo ? .picturesDirectory : .moviesDirectory
        let base = FileManager.default.urls(for: search, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
        #else
        let base = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        #endif
        let dir = base.appendingPathComponent("EV", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private static func timestampedName(prefix: String, ext: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return "\(prefix)-\(formatter.string(from: Date())).\(ext)"
    }

    private static func addToPhotos(fileURL: URL, isVideo: Bool) -> Bool {
        let status: PHAuthorizationStatus
        if #available(macOS 11.0, iOS 14.0, *) {
            status = PHPhotoLibrary.authorizationStatus(for: .addOnly)
        } else {
            status = PHPhotoLibrary.authorizationStatus()
        }
        switch status {
        case .denied, .restricted:
            return false
        case .notDetermined:
            if #available(macOS 11.0, iOS 14.0, *) {
                PHPhotoLibrary.requestAuthorization(for: .addOnly) { next in
                    #if os(iOS)
                    let ok = next == .authorized || next == .limited
                    #else
                    let ok = next == .authorized
                    #endif
                    if ok {
                        performPhotosAdd(fileURL: fileURL, isVideo: isVideo)
                    }
                }
            } else {
                PHPhotoLibrary.requestAuthorization { next in
                    if next == .authorized {
                        performPhotosAdd(fileURL: fileURL, isVideo: isVideo)
                    }
                }
            }
            return false
        default:
            performPhotosAdd(fileURL: fileURL, isVideo: isVideo)
            return true
        }
    }

    private static func performPhotosAdd(fileURL: URL, isVideo: Bool) {
        PHPhotoLibrary.shared().performChanges({
            if isVideo {
                PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: fileURL)
            } else {
                PHAssetChangeRequest.creationRequestForAssetFromImage(atFileURL: fileURL)
            }
        }, completionHandler: { _, _ in })
    }

    private static func jpeg(from cg: CGImage, quality: Double = 0.88) -> Data? {
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else {
            return nil
        }
        CGImageDestinationAddImage(dest, cg, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(dest), out.length > 64 else { return nil }
        return out as Data
    }

    private static func posterJPEGs(from url: URL) -> [Data] {
        let asset = AVURLAsset(url: url)
        let duration = max(CMTimeGetSeconds(asset.duration), 0)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 1280, height: 1280)
        let slack = CMTime(seconds: 0.12, preferredTimescale: 600)
        generator.requestedTimeToleranceBefore = slack
        generator.requestedTimeToleranceAfter = slack
        let fractions: [Double]
        if duration < 1.5 {
            fractions = [0.45]
        } else if duration < 3.5 {
            fractions = [0.18, 0.72]
        } else {
            fractions = [0.12, 0.50, 0.86]
        }
        var images: [Data] = []
        var usedSeconds: [Double] = []
        for fraction in fractions {
            let seconds = duration > 0.05
                ? min(max(duration * fraction, 0), max(duration - 0.03, 0))
                : 0
            if usedSeconds.contains(where: { abs($0 - seconds) < 0.08 }) { continue }
            let time = CMTime(seconds: seconds, preferredTimescale: 600)
            guard let cg = try? generator.copyCGImage(at: time, actualTime: nil),
                  let data = jpeg(from: cg) else { continue }
            images.append(data)
            usedSeconds.append(seconds)
        }
        if images.isEmpty, let fallback = posterJPEG(from: url) {
            images = [fallback]
        }
        return images
    }

    private static func posterJPEG(from url: URL) -> Data? {
        let asset = AVURLAsset(url: url)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 1280, height: 1280)
        let times = [0.4, 0.8, 0.15, 0.0]
        for seconds in times {
            let time = CMTime(seconds: seconds, preferredTimescale: 600)
            guard let cg = try? generator.copyCGImage(at: time, actualTime: nil),
                  let data = jpeg(from: cg) else { continue }
            return data
        }
        return nil
    }

    private static func mergeAnalyses(_ items: [Analysis]) -> Analysis {
        var labels: [String] = []
        var colors: [String] = []
        var ocr: [String] = []
        var faces = 0
        var people = 0
        var lumSum = 0.0
        for item in items {
            for name in item.labels where !labels.contains(where: { $0.caseInsensitiveCompare(name) == .orderedSame }) {
                labels.append(name)
            }
            for name in item.colors where !colors.contains(where: { $0.caseInsensitiveCompare(name) == .orderedSame }) {
                colors.append(name)
            }
            let text = item.ocrText.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty, !ocr.contains(text) {
                ocr.append(text)
            }
            faces = max(faces, item.faceCount)
            people = max(people, item.personCount)
            lumSum += item.luminance
        }
        let luminance = items.isEmpty ? 0.5 : lumSum / Double(items.count)
        let lighting: String
        if luminance >= 0.18 {
            lighting = "normally lit"
        } else if luminance >= 0.10 {
            lighting = "moderately lit"
        } else {
            lighting = "dim"
        }
        return Analysis(
            luminance: luminance,
            labels: Array(labels.prefix(8)),
            ocrText: String(ocr.joined(separator: " ").prefix(400)),
            faceCount: faces,
            personCount: people,
            lighting: lighting,
            colors: Array(colors.prefix(4))
        )
    }

    private struct Analysis {
        var luminance: Double
        var labels: [String]
        var ocrText: String
        var faceCount: Int
        var personCount: Int
        var lighting: String
        var colors: [String]
    }

    private static func analyze(_ data: Data) -> Analysis {
        let stats = pixelStats(data)
        var labels: [String] = []
        var ocr = ""
        var faces = 0
        var people = 0
        if let source = CGImageSourceCreateWithData(data as CFData, nil),
           let image = CGImageSourceCreateImageAtIndex(source, 0, nil) {
            let handler = VNImageRequestHandler(cgImage: image, options: [:])
            let text = VNRecognizeTextRequest()
            text.recognitionLevel = .fast
            text.usesLanguageCorrection = true
            let faceReq = VNDetectFaceRectanglesRequest()
            let humanReq = VNDetectHumanRectanglesRequest()
            let classify = VNClassifyImageRequest()
            try? handler.perform([text, faceReq, humanReq, classify])
            ocr = (text.results ?? [])
                .compactMap { $0.topCandidates(1).first?.string.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
                .joined(separator: " ")
            faces = faceReq.results?.count ?? 0
            people = humanReq.results?.count ?? 0
            labels = (classify.results ?? [])
                .prefix(8)
                .compactMap { observation -> String? in
                    guard observation.confidence >= 0.25 else { return nil }
                    let raw = observation.identifier
                        .replacingOccurrences(of: "_", with: " ")
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    return raw.isEmpty ? nil : raw
                }
        }
        if people == 0 && faces > 0 {
            people = faces
        }
        if faces > 0, !labels.contains(where: { $0.lowercased().contains("person") || $0.lowercased().contains("people") }) {
            labels.insert(faces == 1 ? "person" : "people", at: 0)
        }
        let lighting: String
        if stats.luminance >= 0.18 {
            lighting = "normally lit"
        } else if stats.luminance >= 0.10 {
            lighting = "moderately lit"
        } else {
            lighting = "dim"
        }
        return Analysis(
            luminance: stats.luminance,
            labels: Array(labels.prefix(8)),
            ocrText: String(ocr.prefix(400)),
            faceCount: faces,
            personCount: people,
            lighting: lighting,
            colors: stats.colors
        )
    }

    private struct PixelStats {
        var luminance: Double
        var colors: [String]
    }

    private static func pixelStats(_ data: Data) -> PixelStats {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return PixelStats(luminance: 0.5, colors: []) }
        let width = 32
        let height = 32
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        guard let ctx = CGContext(
            data: &pixels,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else { return PixelStats(luminance: 0.5, colors: []) }
        ctx.interpolationQuality = .low
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        var sum = 0.0
        var counts: [String: Double] = [:]
        var weightSum = 0.0
        let count = width * height
        for y in 0..<height {
            for x in 0..<width {
                let i = (y * width + x) * 4
                let r = Double(pixels[i])
                let g = Double(pixels[i + 1])
                let b = Double(pixels[i + 2])
                sum += (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
                let centerX = x >= 8 && x < 24
                let centerY = y >= 8 && y < 24
                let weight = (centerX && centerY) ? 3.0 : 1.0
                let name = nameRGB(r, g, b)
                counts[name, default: 0] += weight
                weightSum += weight
            }
        }
        let luminance = count == 0 ? 0.5 : sum / Double(count)
        let minimum = max(0.06 * weightSum, 1)
        let ranked = counts.sorted { $0.value > $1.value }
        var colors = ranked.filter { $0.value >= minimum }.prefix(3).map(\.key)
        if colors.isEmpty, let top = ranked.first?.key {
            colors = [top]
        }
        return PixelStats(luminance: luminance, colors: Array(colors))
    }

    private static func nameRGB(_ r: Double, _ g: Double, _ b: Double) -> String {
        let red = r / 255.0
        let green = g / 255.0
        let blue = b / 255.0
        let peak = max(red, green, blue)
        let floor = min(red, green, blue)
        let chroma = peak - floor
        let light = (peak + floor) / 2.0
        let sat = peak <= 0.0001 ? 0.0 : chroma / peak
        if peak < 0.18 { return "black" }
        if floor > 0.82 { return "white" }
        if sat < 0.18 {
            if light > 0.72 { return "white" }
            if light < 0.28 { return "black" }
            return "gray"
        }
        if chroma <= 0.0001 { return "gray" }
        var hue: Double
        if peak == red {
            hue = (green - blue) / chroma
        } else if peak == green {
            hue = 2.0 + (blue - red) / chroma
        } else {
            hue = 4.0 + (red - green) / chroma
        }
        hue = (hue / 6.0).truncatingRemainder(dividingBy: 1.0)
        if hue < 0 { hue += 1 }
        if light < 0.28 && sat < 0.55 { return "brown" }
        if hue < 0.04 || hue >= 0.93 { return light > 0.35 ? "red" : "brown" }
        if hue < 0.10 { return light > 0.45 ? "orange" : "brown" }
        if hue < 0.18 { return "yellow" }
        if hue < 0.45 { return "green" }
        if hue < 0.55 { return "cyan" }
        if hue < 0.73 { return "blue" }
        if hue < 0.85 { return "purple" }
        return "pink"
    }

    private static func meanLuminance(_ data: Data) -> Double {
        pixelStats(data).luminance
    }

    private static func constrainJPEG(_ data: Data, maxLongEdge: CGFloat = 1280, quality: CGFloat = 0.88) -> Data {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return data }
        let width = CGFloat(image.width)
        let height = CGFloat(image.height)
        let longest = max(width, height)
        let scale = longest > maxLongEdge ? maxLongEdge / longest : 1
        if scale == 1, data.count < 1_200_000 {
            return data
        }
        let targetW = max(1, Int(width * scale))
        let targetH = max(1, Int(height * scale))
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

private final class MovieSink: NSObject, AVCaptureFileOutputRecordingDelegate {
    var finish: ((URL?, Error?) -> Void)?
    var finishedURL: URL?
    private(set) var started = false
    private var finished = false

    func fileOutput(
        _ output: AVCaptureFileOutput,
        didStartRecordingTo fileURL: URL,
        from connections: [AVCaptureConnection]
    ) {
        started = true
    }

    func fileOutput(
        _ output: AVCaptureFileOutput,
        didFinishRecordingTo outputFileURL: URL,
        from connections: [AVCaptureConnection],
        error: Error?
    ) {
        guard !finished else { return }
        finished = true
        finishedURL = outputFileURL
        finish?(outputFileURL, error)
    }
}

private final class VideoWriterSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let url: URL
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private(set) var hasStarted = false
    private var finished = false
    private var startWaiters: [CheckedContinuation<Bool, Never>] = []
    private let lock = NSLock()

    init(url: URL) {
        self.url = url
        super.init()
    }

    func waitForStart(timeout: TimeInterval) async -> Bool {
        if hasStarted { return true }
        return await withCheckedContinuation { continuation in
            lock.lock()
            if hasStarted {
                lock.unlock()
                continuation.resume(returning: true)
                return
            }
            startWaiters.append(continuation)
            lock.unlock()
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { [weak self] in
                guard let self else { return }
                self.lock.lock()
                let waiters = self.startWaiters
                self.startWaiters.removeAll()
                let started = self.hasStarted
                self.lock.unlock()
                waiters.forEach { $0.resume(returning: started) }
            }
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard !finished else { return }
        if writer == nil {
            do {
                try prepareWriter(sampleBuffer)
            } catch {
                return
            }
        }
        guard let input, input.isReadyForMoreMediaData else { return }
        if input.append(sampleBuffer) {
            markStarted()
        }
    }

    func requestStop() {
        finish { _ in }
    }

    func finish(completion: @escaping (Result<URL, Error>) -> Void) {
        lock.lock()
        if finished {
            lock.unlock()
            if let writer, writer.status == .completed {
                completion(.success(url))
            } else {
                completion(.failure(CameraManager.CaptureError.failed("Recording produced no file.")))
            }
            return
        }
        finished = true
        lock.unlock()
        markStarted()
        guard let writer, let input else {
            completion(.failure(CameraManager.CaptureError.failed("Recording produced no file.")))
            return
        }
        input.markAsFinished()
        writer.finishWriting {
            if writer.status == .completed {
                completion(.success(self.url))
            } else {
                completion(.failure(writer.error ?? CameraManager.CaptureError.failed("Recording produced no file.")))
            }
        }
    }

    private func markStarted() {
        lock.lock()
        hasStarted = true
        let waiters = startWaiters
        startWaiters.removeAll()
        lock.unlock()
        waiters.forEach { $0.resume(returning: true) }
    }

    private func prepareWriter(_ sample: CMSampleBuffer) throws {
        try? FileManager.default.removeItem(at: url)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        var width = 1280
        var height = 720
        if let format = CMSampleBufferGetFormatDescription(sample) {
            let dims = CMVideoFormatDescriptionGetDimensions(format)
            width = max(1, Int(dims.width))
            height = max(1, Int(dims.height))
        }
        let input = AVAssetWriterInput(
            mediaType: .video,
            outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: width,
                AVVideoHeightKey: height,
            ]
        )
        input.expectsMediaDataInRealTime = true
        if let format = CMSampleBufferGetFormatDescription(sample),
           let transform = Self.preferredTransform(from: format) {
            input.transform = transform
        }
        guard writer.canAdd(input) else {
            throw CameraManager.CaptureError.failed("Could not add video writer input.")
        }
        writer.add(input)
        guard writer.startWriting() else {
            throw CameraManager.CaptureError.failed(writer.error?.localizedDescription ?? "Could not start video writer.")
        }
        writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sample))
        self.writer = writer
        self.input = input
    }

    private static func preferredTransform(from format: CMFormatDescription) -> CGAffineTransform? {
        _ = format
        return nil
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
