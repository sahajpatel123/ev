import Foundation
#if canImport(Vision)
import Vision
#endif
#if canImport(VisionKit)
import VisionKit
#endif
#if canImport(UIKit)
import UIKit
#endif
#if canImport(ObjectiveC)
import ObjectiveC
#endif

// Cycle — iPhone-only, backward compat: additive document capture bridge.
// Body-not-brain: no model calls, no DeviceAuth token use, no auto-send.
// Uses the system VNDocumentCameraViewController where available; OCR (Vision)
// and PDF assembly are separate hooks the broker/integration calls after the
// user confirms. Capture only stages bytes + text on device.
// Wiring hook: CapabilityBroker.handle can call DocScanBridge.present / .ocrHook /
// .pdfHook; no broker edits here.

/// Pure, platform-independent staging of a finished capture.
/// Nothing is sent anywhere; `executed` stays false until the user confirms.
enum DocScanBridge {
    static let maxPages = 24

    /// Broker-style payload for a finished capture. Counts only — images/PDF
    /// stay on device until a confirmed broker path moves them.
    static func payload(pageCount: Int, hasPDF: Bool, textChars: Int, unsupported: Bool = false) -> [String: Any] {
        if unsupported {
            return [
                "ok": false,
                "result": "FAILED",
                "failure": "ACTION_UNAVAILABLE",
                "note": "Document scanner not available on this device.",
            ]
        }
        return [
            "ok": true,
            "result": "EXECUTED",
            "executed": false,
            "verified": true,
            "pages": pageCount,
            "has_pdf": hasPDF,
            "text_chars": textChars,
            "opened": false,
            "note": "Capture staged on device. Nothing was sent; confirm before upload/share.",
            "sent_to_model": false,
        ]
    }
}

#if os(iOS)
/// OCR hook: Vision text recognition over scanned page images.
/// Runs on device; returns concatenated recognized text for broker staging.
@available(iOS 17.0, *)
enum DocScanOCRHook {
    static func recognizeText(in images: [UIImage]) async -> String {
        var parts: [String] = []
        for image in images.prefix(DocScanBridge.maxPages) {
            guard let cgImage = image.cgImage else { continue }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = true
            let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
            try? await handler.perform([request])
            let page = (request.results ?? [])
                .compactMap { $0.topCandidates(1).first?.string }
                .joined(separator: "\n")
            if !page.isEmpty { parts.append(page) }
        }
        return parts.joined(separator: "\n\n")
    }
}

/// PDF hook: assembles scanned page images into a single PDF file under tmp.
/// Returns the file URL, or nil when assembly fails. Caller owns cleanup.
@available(iOS 17.0, *)
enum DocScanPDFHook {
    static func assemblePDF(from images: [UIImage]) -> URL? {
        let pages = Array(images.prefix(DocScanBridge.maxPages))
        guard !pages.isEmpty else { return nil }
        #if canImport(PDFKit)
        let document = PDFDocument()
        for (index, image) in pages.enumerated() {
            guard let page = PDFPage(image: image) else { continue }
            document.insert(page, at: index)
        }
        guard document.pageCount > 0 else { return nil }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("evie-scan-\(UUID().uuidString).pdf")
        guard document.write(to: url) else { return nil }
        return url
        #else
        return nil
        #endif
    }
}

/// Foreground-only document scanner host. Uses the system VisionKit camera UI
/// where supported; completion fires once with a broker-style payload.
@available(iOS 17.0, *)
final class DocScanHost: NSObject, VNDocumentCameraViewControllerDelegate {
    private var completion: (([String: Any]) -> Void)?

    @MainActor
    static func present(completion: @escaping ([String: Any]) -> Void) {
        guard VNDocumentCameraViewController.isSupported else {
            completion(DocScanBridge.payload(pageCount: 0, hasPDF: false, textChars: 0, unsupported: true))
            return
        }
        guard let presenter = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .flatMap({ $0.windows })
            .first(where: { $0.isKeyWindow })?
            .rootViewController
        else {
            completion(["ok": false, "result": "FAILED", "failure": "EXECUTION_FAILED"])
            return
        }
        let host = DocScanHost()
        host.completion = completion
        // Retain the host for the scan lifetime; released in the delegate callbacks.
        objc_setAssociatedObject(
            presenter, "evie.docScanHost",
            host, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        let scanner = VNDocumentCameraViewController()
        scanner.delegate = host
        presenter.present(scanner, animated: true)
    }

    func documentCameraViewController(
        _ controller: VNDocumentCameraViewController,
        didFinishWith scan: VNDocumentCameraScan
    ) {
        var images: [UIImage] = []
        for index in 0..<min(scan.pageCount, DocScanBridge.maxPages) {
            images.append(scan.imageOfPage(at: index))
        }
        controller.dismiss(animated: true) { [images, weak self] in
            guard let self else { return }
            Task { @MainActor in
                let text = await DocScanOCRHook.recognizeText(in: images)
                let pdfURL = DocScanPDFHook.assemblePDF(from: images)
                if let pdfURL {
                    UserDefaults.standard.set(pdfURL.path, forKey: "evie.docscan.last_pdf")
                }
                UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "evie.docscan.last_at")
                let payload = DocScanBridge.payload(
                    pageCount: images.count,
                    hasPDF: pdfURL != nil,
                    textChars: text.count
                )
                self.finish(with: payload, from: controller)
            }
        }
    }

    func documentCameraViewControllerDidCancel(_ controller: VNDocumentCameraViewController) {
        controller.dismiss(animated: true) { [weak self] in
            self?.finish(
                with: ["ok": false, "result": "FAILED", "failure": "ACTION_UNAVAILABLE"],
                from: controller
            )
        }
    }

    func documentCameraViewController(
        _ controller: VNDocumentCameraViewController,
        didFailWithError error: Error
    ) {
        controller.dismiss(animated: true) { [weak self] in
            self?.finish(
                with: ["ok": false, "result": "FAILED", "failure": "EXECUTION_FAILED"],
                from: controller
            )
        }
    }

    private func finish(with payload: [String: Any], from controller: VNDocumentCameraViewController) {
        let completion = self.completion
        self.completion = nil
        if let presenter = controller.presentingViewController {
            objc_setAssociatedObject(presenter, "evie.docScanHost", nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        }
        completion?(payload)
    }
}

@available(iOS 17.0, *)
extension DocScanBridge {
    /// Presents the system document scanner from the key window's root VC.
    /// Availability is checked first; unsupported devices get an
    /// ACTION_UNAVAILABLE payload without presenting anything.
    @MainActor
    static func present(completion: @escaping ([String: Any]) -> Void) {
        DocScanHost.present(completion: completion)
    }

    /// Whether the system scanner can run on this device (physical check).
    static var isSupported: Bool {
        VNDocumentCameraViewController.isSupported
    }
}
#endif
