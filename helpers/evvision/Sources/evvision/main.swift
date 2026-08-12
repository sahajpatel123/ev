import AppKit
import AVFoundation
import CoreGraphics
import Darwin
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

let version = "1.1.0"

// MARK: - Errors

enum ExitCode: Int32 {
    case ok = 0
    case generic = 2
    case permission = 3
}

enum EvError: Error, CustomStringConvertible {
    case invalidInput(String)
    case engine(String)
    case screenRecordingDenied(String)
    case cameraDenied(String)
    case noCamera(String)
    case noWindow(String)

    var description: String {
        switch self {
        case .invalidInput(let message): return message
        case .engine(let message): return message
        case .screenRecordingDenied(let message): return message
        case .cameraDenied(let message): return message
        case .noCamera(let message): return message
        case .noWindow(let message): return message
        }
    }

    var code: String {
        switch self {
        case .invalidInput: return "invalid_input"
        case .engine: return "engine_error"
        case .screenRecordingDenied: return "screen_recording_denied"
        case .cameraDenied: return "camera_denied"
        case .noCamera: return "no_camera"
        case .noWindow: return "no_window"
        }
    }

    var exitCode: ExitCode {
        switch self {
        case .screenRecordingDenied, .cameraDenied: return .permission
        default: return .generic
        }
    }
}

func logAction(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write(Data("[\(stamp)] \(message)\n".utf8))
}

func peakRSSBytes() -> Int64 {
    var usage = rusage()
    if getrusage(RUSAGE_SELF, &usage) == 0 {
        return Int64(usage.ru_maxrss)
    }
    return 0
}

func peakRSSMB() -> Double {
    return Double(peakRSSBytes()) / (1024.0 * 1024.0)
}

// MARK: - JSON helpers

func jsonOrNull<T>(_ value: T?) -> Any {
    return value.map { $0 as Any } ?? NSNull()
}

func printJSON(_ object: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func printError(_ error: EvError) {
    printJSON(["error": ["code": error.code, "message": error.description]])
}

func parseLevel(_ raw: String) throws -> VNRequestTextRecognitionLevel {
    switch raw.lowercased() {
    case "fast":
        return .fast
    case "accurate":
        return .accurate
    default:
        throw EvError.invalidInput("unknown OCR level '\(raw)' (fast|accurate)")
    }
}

// MARK: - Accurate-level cache

let accurateBrokenMarker = FileManager.default.temporaryDirectory
    .appendingPathComponent("evvision-accurate-broken")

func accurateKnownBroken() -> Bool {
    return FileManager.default.fileExists(atPath: accurateBrokenMarker.path)
}

func markAccurateBroken() {
    try? Data().write(to: accurateBrokenMarker, options: .atomic)
}

func markAccurateWorking() {
    try? FileManager.default.removeItem(at: accurateBrokenMarker)
}

// MARK: - OCR

struct OCRLine {
    let text: String
    let confidence: Double
    let box: [String: Double]
}

struct OCRResult {
    let text: String?
    let lines: [OCRLine]
    let pageCount: Int
}

func ocrRequest(level: VNRequestTextRecognitionLevel = .accurate) -> VNRecognizeTextRequest {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = level
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US", "en-GB"]
    return request
}

func recognize(
    level: VNRequestTextRecognitionLevel,
    handler: VNImageRequestHandler
) throws -> [VNRecognizedTextObservation] {
    let request = ocrRequest(level: level)
    try handler.perform([request])
    return request.results ?? []
}

func recognizeWithFallback(
    level: VNRequestTextRecognitionLevel,
    makeHandler: (VNRequestTextRecognitionLevel) throws -> VNImageRequestHandler
) throws -> [VNRecognizedTextObservation] {
    if level == .fast {
        return try recognize(level: .fast, handler: makeHandler(.fast))
    }
    if !accurateKnownBroken() {
        do {
            let observed = try recognize(level: .accurate, handler: makeHandler(.accurate))
            markAccurateWorking()
            return observed
        } catch {
            let nsError = error as NSError
            logAction(
                "Accurate OCR failed (\(nsError.domain) \(nsError.code)); "
                + "marking broken and retrying fast level"
            )
            markAccurateBroken()
        }
    } else {
        logAction("Accurate OCR known broken on this host; using fast level")
    }
    do {
        return try recognize(level: .fast, handler: makeHandler(.fast))
    } catch {
        logAction("Fast OCR also failed; re-raising accurate-level error")
        throw error
    }
}

func lines(from observations: [VNRecognizedTextObservation]) -> [OCRLine] {
    var result: [OCRLine] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        // Vision uses bottom-left origin; expose top-left normalized coordinates.
        let y = Double(1.0 - box.origin.y - box.size.height)
        result.append(
            OCRLine(
                text: candidate.string,
                confidence: Double(candidate.confidence),
                box: [
                    "x": Double(box.origin.x),
                    "y": y,
                    "width": Double(box.size.width),
                    "height": Double(box.size.height),
                ]
            )
        )
    }
    return result
}

func ocrImage(
    _ image: CGImage,
    level: VNRequestTextRecognitionLevel = .accurate
) throws -> OCRResult {
    let observed = try recognizeWithFallback(level: level) { level in
        VNImageRequestHandler(cgImage: image, options: [:])
    }
    let result = lines(from: observed)
    return OCRResult(
        text: result.isEmpty ? nil : result.map(\.text).joined(separator: "\n"),
        lines: result,
        pageCount: 1
    )
}

func ocrPDF(
    _ url: URL,
    level: VNRequestTextRecognitionLevel = .accurate
) throws -> OCRResult {
    guard let pdf = CGPDFDocument(url as CFURL) else {
        throw EvError.invalidInput("Could not open PDF at \(url.path)")
    }
    var all: [OCRLine] = []
    for pageIndex in 1...pdf.numberOfPages {
        guard let page = pdf.page(at: pageIndex) else { continue }
        let image = try renderPDFPage(page)
        let ocr = try ocrImage(image, level: level)
        all.append(contentsOf: ocr.lines)
    }
    return OCRResult(
        text: all.isEmpty ? nil : all.map(\.text).joined(separator: "\n"),
        lines: all,
        pageCount: pdf.numberOfPages
    )
}

func renderPDFPage(_ page: CGPDFPage) throws -> CGImage {
    let box = page.getBoxRect(.mediaBox)
    let scale: CGFloat = 2.0
    let width = max(1, Int(box.width * scale))
    let height = max(1, Int(box.height * scale))
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        throw EvError.engine("Could not create PDF page rendering context")
    }
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.scaleBy(x: scale, y: scale)
    context.drawPDFPage(page)
    guard let image = context.makeImage() else {
        throw EvError.engine("Could not render PDF page")
    }
    return image
}

func runOCR(
    path: String,
    level: VNRequestTextRecognitionLevel = .accurate
) throws -> [String: Any] {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: path) else {
        throw EvError.invalidInput("No file at \(path)")
    }
    let startedAt = Date()
    logAction("OCR start: \(path)")
    let result: OCRResult
    if url.pathExtension.lowercased() == "pdf" {
        result = try ocrPDF(url, level: level)
    } else {
        do {
            let observed = try recognizeWithFallback(level: level) { level in
                VNImageRequestHandler(url: url, options: [:])
            }
            result = OCRResult(
                text: observed.isEmpty
                    ? nil
                    : observed.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n"),
                lines: lines(from: observed),
                pageCount: 1
            )
        } catch {
            // Fall back to a CGImage-backed handler (some formats need it).
            guard
                let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
            else {
                throw EvError.invalidInput("Could not decode image at \(path)")
            }
            result = try ocrImage(image, level: level)
        }
    }
    logAction("OCR done: \(result.lines.count) line(s)")
    let elapsedMs = Date().timeIntervalSince(startedAt) * 1000.0
    return [
        "provider": "apple_vision",
        "text": jsonOrNull(result.text),
        "lines": result.lines.map { line in
            [
                "text": line.text,
                "confidence": line.confidence,
                "bounding_box": line.box,
            ]
        },
        "page_count": result.pageCount,
        "elapsed_ms": round(elapsedMs * 100.0) / 100.0,
        "peak_rss_mb": round(peakRSSMB() * 100.0) / 100.0,
    ]
}

// MARK: - Downscale + persist

func downscaled(_ image: CGImage, maxDimension: Int = 1280) -> CGImage {
    let width = image.width
    let height = image.height
    let longest = max(width, height)
    guard longest > maxDimension else { return image }
    let scale = Double(maxDimension) / Double(longest)
    let targetWidth = max(1, Int(Double(width) * scale))
    let targetHeight = max(1, Int(Double(height) * scale))
    guard
        let context = CGContext(
            data: nil,
            width: targetWidth,
            height: targetHeight,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    else {
        return image
    }
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: targetWidth, height: targetHeight))
    return context.makeImage() ?? image
}

func persistPNG(_ image: CGImage, to path: String) throws {
    let url = URL(fileURLWithPath: path)
    guard let destination = CGImageDestinationCreateWithURL(
        url as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else {
        throw EvError.engine("Could not create PNG destination at \(path)")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        throw EvError.engine("Could not write PNG to \(path)")
    }
    logAction("Persisted frame to \(path)")
}

// MARK: - Screen capture

func captureScreen(
    persistPath: String?,
    level: VNRequestTextRecognitionLevel = .accurate
) async throws -> [String: Any] {
    let startedAt = Date()
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.current
    } catch {
        let nsError = error as NSError
        if nsError.domain == SCStreamError.errorDomain {
            throw EvError.screenRecordingDenied(
                "Screen Recording permission denied: \(nsError.localizedDescription). Grant EV Screen Recording in System Settings > Privacy & Security."
            )
        }
        throw EvError.engine("Could not query shareable content: \(error.localizedDescription)")
    }

    let workspace = NSWorkspace.shared
    guard let frontmost = workspace.frontmostApplication else {
        throw EvError.engine("Could not determine the frontmost application")
    }
    var window = content.windows.first {
        $0.isOnScreen && $0.owningApplication?.processID == frontmost.processIdentifier
    }
    if window == nil {
        window = content.windows.first {
            $0.isOnScreen && $0.owningApplication?.applicationName == frontmost.localizedName
        }
    }
    guard let window = window else {
        throw EvError.noWindow("No on-screen window found for \(frontmost.localizedName ?? "frontmost app")")
    }

    let filter = SCContentFilter(desktopIndependentWindow: window)
    let config = SCStreamConfiguration()
    // SCScreenshotManager captures at the requested size; requesting a smaller
    // size crops instead of scaling, so capture full-size and downscale.
    let windowWidth = max(1, Int(window.frame.width))
    let windowHeight = max(1, Int(window.frame.height))
    config.width = windowWidth
    config.height = windowHeight
    config.capturesAudio = false
    config.showsCursor = false
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

    logAction("Screen capture start: \(frontmost.localizedName ?? "unknown") / \(window.title ?? "untitled")")
    let raw: CGImage
    do {
        raw = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
    } catch {
        let nsError = error as NSError
        if nsError.domain == SCStreamError.errorDomain {
            throw EvError.screenRecordingDenied(
                "Screen Recording permission denied: \(nsError.localizedDescription). Grant EV Screen Recording in System Settings > Privacy & Security."
            )
        }
        throw EvError.engine("Screen capture failed: \(error.localizedDescription)")
    }

    let small = downscaled(raw)
    let ocr = try ocrImage(small, level: level)
    var persisted = false
    var persistPathValue: String? = nil
    if let persistPath = persistPath {
        try persistPNG(small, to: persistPath)
        persisted = true
        persistPathValue = persistPath
    }
    logAction("Screen capture done: \(raw.width)x\(raw.height) -> \(small.width)x\(small.height)")
    let elapsedMs = Date().timeIntervalSince(startedAt) * 1000.0
    return [
        "provider": "screen_capture",
        "app": jsonOrNull(frontmost.localizedName),
        "window": jsonOrNull(window.title),
        "captured": true,
        "persisted": persisted,
        "persist_path": jsonOrNull(persistPathValue),
        "pixel_count": small.width * small.height,
        "elapsed_ms": round(elapsedMs * 100.0) / 100.0,
        "peak_rss_mb": round(peakRSSMB() * 100.0) / 100.0,
        "ocr": [
            "text": jsonOrNull(ocr.text),
            "lines": ocr.lines.map { line in
                [
                    "text": line.text,
                    "confidence": line.confidence,
                    "bounding_box": line.box,
                ]
            },
        ],
    ]
}

// MARK: - Camera capture

final class PhotoDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    var continuation: CheckedContinuation<Data?, Error>?

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        if let error = error {
            continuation?.resume(throwing: error)
            return
        }
        continuation?.resume(returning: photo.fileDataRepresentation())
    }
}

func captureCamera(persistPath: String?) async throws -> [String: Any] {
    let startedAt = Date()
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized:
        break
    case .notDetermined:
        let granted = await AVCaptureDevice.requestAccess(for: .video)
        guard granted else {
            throw EvError.cameraDenied(
                "Camera permission denied. Grant EV camera access in System Settings > Privacy & Security."
            )
        }
    default:
        throw EvError.cameraDenied(
            "Camera permission denied. Grant EV camera access in System Settings > Privacy & Security."
        )
    }

    guard let device = AVCaptureDevice.default(for: .video) else {
        throw EvError.noCamera("No video capture device is available")
    }

    let session = AVCaptureSession()
    session.beginConfiguration()
    do {
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw EvError.engine("Could not add camera input")
        }
        session.addInput(input)
    } catch let error as EvError {
        session.commitConfiguration()
        throw error
    } catch {
        session.commitConfiguration()
        throw EvError.engine("Could not open camera: \(error.localizedDescription)")
    }
    let output = AVCapturePhotoOutput()
    guard session.canAddOutput(output) else {
        session.commitConfiguration()
        throw EvError.engine("Could not add photo output")
    }
    session.addOutput(output)
    session.commitConfiguration()

    logAction("Camera capture start (single frame, explicit request): \(device.localizedName)")
    session.startRunning()
    guard session.isRunning else {
        throw EvError.engine("Camera session failed to start")
    }
    let delegate = PhotoDelegate()
    let data: Data?
    do {
        data = try await withCheckedThrowingContinuation { continuation in
            delegate.continuation = continuation
            output.capturePhoto(with: AVCapturePhotoSettings(), delegate: delegate)
        }
    } catch {
        session.stopRunning()
        throw EvError.engine("Camera capture failed: \(error.localizedDescription)")
    }
    session.stopRunning()

    guard let data = data else {
        throw EvError.engine("Camera returned no photo data")
    }

    var persisted = false
    var persistPathValue: String? = nil
    var pixelCount = 0
    if let persistPath = persistPath {
        let url = URL(fileURLWithPath: persistPath)
        do {
            try data.write(to: url, options: .atomic)
            persisted = true
            persistPathValue = persistPath
            logAction("Persisted camera frame to \(persistPath)")
        } catch {
            throw EvError.engine("Could not write camera frame to \(persistPath): \(error.localizedDescription)")
        }
    }
    if let source = CGImageSourceCreateWithData(data as CFData, nil),
       let image = CGImageSourceCreateImageAtIndex(source, 0, nil) {
        pixelCount = image.width * image.height
    }
    logAction("Camera capture done")
    let elapsedMs = Date().timeIntervalSince(startedAt) * 1000.0
    return [
        "provider": "camera_capture",
        "device": device.localizedName,
        "captured": true,
        "persisted": persisted,
        "persist_path": jsonOrNull(persistPathValue),
        "pixel_count": pixelCount,
        "elapsed_ms": round(elapsedMs * 100.0) / 100.0,
        "peak_rss_mb": round(peakRSSMB() * 100.0) / 100.0,
    ]
}

// MARK: - Self-test OCR

func normalizedCharacters(_ text: String) -> [Character] {
    return text.lowercased().filter { $0.isLetter || $0.isNumber }
}

func editDistance(_ a: [Character], _ b: [Character]) -> Int {
    var previous = Array(0...b.count)
    for (i, ca) in a.enumerated() {
        var current = [i + 1]
        for (j, cb) in b.enumerated() {
            let cost = ca == cb ? 0 : 1
            current.append(min(previous[j + 1] + 1, current[j] + 1, previous[j] + cost))
        }
        previous = current
    }
    return previous[b.count]
}

func characterAccuracy(got: String, expected: String) -> Double {
    let g = normalizedCharacters(got)
    let e = normalizedCharacters(expected)
    guard !e.isEmpty else { return g.isEmpty ? 1.0 : 0.0 }
    return max(0.0, 1.0 - Double(editDistance(g, e)) / Double(e.count))
}

func renderTextPNG(text: String, to url: URL, fontSize: CGFloat, backgroundColor: NSColor) throws {
    let width = 720
    let height = 160
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: width,
        pixelsHigh: height,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw EvError.engine("Could not allocate bitmap for self-test")
    }
    NSGraphicsContext.saveGraphicsState()
    guard let context = NSGraphicsContext(bitmapImageRep: rep) else {
        NSGraphicsContext.restoreGraphicsState()
        throw EvError.engine("Could not create drawing context")
    }
    NSGraphicsContext.current = context
    backgroundColor.setFill()
    NSRect(x: 0, y: 0, width: width, height: height).fill()
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize),
        .foregroundColor: NSColor.black,
    ]
    text.draw(at: NSPoint(x: 24, y: (CGFloat(height) - fontSize) / 2), withAttributes: attributes)
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else {
        throw EvError.engine("Could not encode self-test PNG")
    }
    try png.write(to: url, options: .atomic)
}

func runSelfTestOCR(directory: String) throws -> [String: Any] {
    let dir = URL(fileURLWithPath: directory, isDirectory: true)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let samples: [(String, CGFloat, NSColor)] = [
        ("The quick brown fox jumps over the lazy dog", 34, .white),
        ("INVOICE EV-42 TOTAL 100 USD", 40, .white),
        ("Meeting agenda: quarterly planning at 10 AM", 32, .white),
        ("Receipt #9041 - coffee and a croissant", 36, .white),
        ("Prescription: take one tablet daily with food", 34, .white),
        ("Passport number AB1234567 expires 2030", 38, .white),
        ("EV perception is permissioned and local first", 30, .white),
        ("Screen capture never stores raw pixels by default", 28, .white),
        ("Object detection returns boxes and scores from ONNX", 30, .white),
        ("Scene labels are suggestions pending human confirmation", 28, .white),
    ]
    var perImage: [[String: Any]] = []
    var total = 0.0
    for (index, sample) in samples.enumerated() {
        let path = dir.appendingPathComponent("sample-\(index + 1).png")
        try renderTextPNG(
            text: sample.0,
            to: path,
            fontSize: sample.1,
            backgroundColor: sample.2
        )
        let ocr = try runOCR(path: path.path)
        let recognized = (ocr["text"] as? String) ?? ""
        let accuracy = characterAccuracy(got: recognized, expected: sample.0)
        total += accuracy
        perImage.append([
            "index": index + 1,
            "accuracy": accuracy,
            "text": recognized,
            "expected": sample.0,
        ])
    }
    let average = total / Double(samples.count)
    logAction("Self-test OCR average accuracy: \(average)")
    return [
        "images": samples.count,
        "avg_character_accuracy": average,
        "per_image": perImage,
    ]
}

func renderTextPDF(text: String, to url: URL) throws {
    var mediaBox = CGRect(x: 0, y: 0, width: 612, height: 420)
    guard let context = CGContext(url as CFURL, mediaBox: &mediaBox, nil) else {
        throw EvError.engine("Could not create PDF context for self-test")
    }
    context.beginPDFPage(nil)
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(mediaBox)
    let font = CTFontCreateWithName("Helvetica" as CFString, 14, nil)
    let lines = text.components(separatedBy: "\n")
    var y = 360
    for line in lines {
        let attributes = [
            kCTFontAttributeName: font,
            kCTForegroundColorAttributeName: CGColor(red: 0, green: 0, blue: 0, alpha: 1),
        ] as CFDictionary
        guard let attributed = CFAttributedStringCreate(nil, line as CFString, attributes) else {
            throw EvError.engine("Could not create attributed string for PDF self-test")
        }
        let ctLine = CTLineCreateWithAttributedString(attributed)
        context.textPosition = CGPoint(x: 40, y: CGFloat(y))
        CTLineDraw(ctLine, context)
        y -= 34
    }
    context.endPDFPage()
    context.closePDF()
}

func runSelfTestPDF(directory: String) throws -> [String: Any] {
    let dir = URL(fileURLWithPath: directory, isDirectory: true)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let samples = [
        "The quick brown fox jumps over the lazy dog\nInvoice EV-42 total 100 USD",
        "Meeting agenda: quarterly planning at 10 AM\nRoom 3B, second floor",
        "Receipt #9041 - coffee and a croissant\nPayment method: card",
        "Prescription: take one tablet daily with food\nRefill before Friday",
        "Passport number AB1234567 expires 2030\nIssued in New Delhi",
        "EV perception is permissioned and local first\nRaw media never leaves the device",
        "Screen capture never stores raw pixels by default\nFrontmost window only",
        "Object detection returns boxes and scores from ONNX\nClass confidence is real",
        "Scene labels are suggestions pending human confirmation\nConfirm before memory",
        "Face detection produces boxes and landmarks only\nIdentity belongs to Agent 7",
    ]
    var perDocument: [[String: Any]] = []
    var total = 0.0
    for (index, text) in samples.enumerated() {
        let path = dir.appendingPathComponent("document-\(index + 1).pdf")
        try renderTextPDF(text: text, to: path)
        let ocr = try runOCR(path: path.path)
        let recognized = (ocr["text"] as? String) ?? ""
        let accuracy = characterAccuracy(got: recognized, expected: text)
        total += accuracy
        perDocument.append([
            "index": index + 1,
            "accuracy": accuracy,
            "text": recognized,
            "expected": text,
        ])
    }
    let average = total / Double(samples.count)
    logAction("Self-test PDF average accuracy: \(average)")
    return [
        "documents": samples.count,
        "avg_character_accuracy": average,
        "per_document": perDocument,
    ]
}

// MARK: - CLI

func usage() {
    print(
        """
        evvision \(version)
        Usage:
          evvision ocr <input-path> [--level fast|accurate]
          evvision screen [--persist <path>] [--level fast|accurate]
          evvision camera --once [--persist <path>]
          evvision --selftest-ocr <tmpdir>
          evvision --selftest-pdf <tmpdir>
          evvision --version
        """,
        to: &standardError
    )
}

struct StandardErrorStream: TextOutputStream {
    mutating func write(_ string: String) {
        FileHandle.standardError.write(Data(string.utf8))
    }
}

var standardError = StandardErrorStream()

@main
struct EVVision {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        // ScreenCaptureKit/NSWorkspace need a WindowServer connection; without
        // this, CGS can abort the process with CGS_REQUIRE_INIT.
        if args.first == "screen" {
            _ = NSApplication.shared
        }
        do {
            switch args.first {
            case "ocr":
                var path: String? = nil
                var level = VNRequestTextRecognitionLevel.accurate
                var index = 1
                while index < args.count {
                    if args[index] == "--level", index + 1 < args.count {
                        level = try parseLevel(args[index + 1])
                        index += 2
                    } else if path == nil {
                        path = args[index]
                        index += 1
                    } else {
                        usage()
                        exit(ExitCode.generic.rawValue)
                    }
                }
                guard let path = path else {
                    usage()
                    exit(ExitCode.generic.rawValue)
                }
                printJSON(try runOCR(path: path, level: level))
            case "screen":
                var persistPath: String? = nil
                var level = VNRequestTextRecognitionLevel.accurate
                var index = 1
                while index < args.count {
                    if args[index] == "--persist", index + 1 < args.count {
                        persistPath = args[index + 1]
                        index += 2
                    } else if args[index] == "--level", index + 1 < args.count {
                        level = try parseLevel(args[index + 1])
                        index += 2
                    } else {
                        usage()
                        exit(ExitCode.generic.rawValue)
                    }
                }
                printJSON(try await captureScreen(persistPath: persistPath, level: level))
            case "camera":
                guard args.count >= 2, args[1] == "--once" else {
                    usage()
                    exit(ExitCode.generic.rawValue)
                }
                var persistPath: String? = nil
                var index = 2
                while index < args.count {
                    if args[index] == "--persist", index + 1 < args.count {
                        persistPath = args[index + 1]
                        index += 2
                    } else {
                        usage()
                        exit(ExitCode.generic.rawValue)
                    }
                }
                printJSON(try await captureCamera(persistPath: persistPath))
            case "--selftest-ocr":
                guard args.count == 2 else { usage(); exit(ExitCode.generic.rawValue) }
                let result = try runSelfTestOCR(directory: args[1])
                let average = (result["avg_character_accuracy"] as? Double) ?? 0.0
                printJSON(result)
                if average < 0.95 {
                    logAction("Self-test failed: average accuracy \(average) < 0.95")
                    exit(ExitCode.generic.rawValue)
                }
            case "--selftest-pdf":
                guard args.count == 2 else { usage(); exit(ExitCode.generic.rawValue) }
                let result = try runSelfTestPDF(directory: args[1])
                let average = (result["avg_character_accuracy"] as? Double) ?? 0.0
                printJSON(result)
                if average < 0.95 {
                    logAction("PDF self-test failed: average accuracy \(average) < 0.95")
                    exit(ExitCode.generic.rawValue)
                }
            case "--version":
                print("evvision \(version)")
            default:
                usage()
                exit(ExitCode.generic.rawValue)
            }
        } catch let error as EvError {
            logAction("Error: \(error.description)")
            printError(error)
            exit(error.exitCode.rawValue)
        } catch {
            logAction("Error: \(error.localizedDescription)")
            printError(.engine(error.localizedDescription))
            exit(ExitCode.generic.rawValue)
        }
    }
}
