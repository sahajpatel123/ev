import Foundation
#if canImport(AVFoundation)
import AVFoundation
#endif
#if canImport(UIKit)
import UIKit
#endif

// Cycle — iPhone-only, backward compat: additive foreground QR/barcode recognition bridge.
// Body-not-brain: no model calls, no DeviceAuth token use, no auto-execute.
// The scanner only reports what it saw; opening/handling a URL stays on the
// CapabilityBroker openURL path (user-confirmed), never here.
// Wiring hook: CapabilityBroker.handle can call QRScanBridge.present / .classify; no broker edits here.

/// Pure, platform-independent classification of a scanned string value.
/// Safe URL handling: only http/https are open candidates; everything else is
/// reported as text or a handoff candidate. Never opens anything itself.
enum QRScanBridge {
    /// Kinds: "url" (http/https), "handoff" (tel/sms/facetime/maps), "text" (everything else).
    static func classify(_ raw: String) -> [String: String] {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return ["kind": "text", "raw_value": "", "safe_to_open": "false", "opened": "false"]
        }
        let lower = trimmed.lowercased()
        if lower.hasPrefix("https://") || lower.hasPrefix("http://") {
            // Reject non-default ports / credentials baked into QR URLs as unsafe.
            let safe = isSafeHTTPURL(trimmed)
            return [
                "kind": "url",
                "raw_value": trimmed,
                "safe_to_open": safe ? "true" : "false",
                "opened": "false",
            ]
        }
        if lower.hasPrefix("tel:") || lower.hasPrefix("sms:")
            || lower.hasPrefix("facetime:") || lower.hasPrefix("facetime-audio:")
            || lower.hasPrefix("https://maps.apple.com") || lower.hasPrefix("http://maps.apple.com")
        {
            return [
                "kind": "handoff",
                "raw_value": trimmed,
                "safe_to_open": "false",
                "opened": "false",
            ]
        }
        return [
            "kind": "text",
            "raw_value": trimmed,
            "safe_to_open": "false",
            "opened": "false",
        ]
    }

    private static func isSafeHTTPURL(_ raw: String) -> Bool {
        guard let components = URLComponents(string: raw),
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil
        else { return false }
        if let port = components.port, port != 80, port != 443, port != 0 { return false }
        return true
    }

    /// Broker-style payload for a scanned value. `executed` is always false:
    /// scanning observes; it never acts.
    static func payload(for raw: String) -> [String: Any] {
        let classified = classify(raw)
        return [
            "ok": true,
            "result": "EXECUTED",
            "executed": false,
            "verified": true,
            "kind": classified["kind"] ?? "text",
            "raw_value": classified["raw_value"] ?? "",
            "safe_to_open": classified["safe_to_open"] == "true",
            "opened": false,
            "note": "Foreground scan only. Nothing was opened; confirm before openURL.",
            "sent_to_model": false,
        ]
    }
}

#if os(iOS)
/// Foreground-only scanner view controller. Presented modally over the key window;
/// capture session starts on appear and stops on dismiss. No background scanning.
@available(iOS 17.0, *)
final class QRScannerViewController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onScanned: ((String) -> Void)?

    private var session: AVCaptureSession?
    private var didReport = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        let cancel = UIButton(type: .system)
        cancel.setTitle("Cancel", for: .normal)
        cancel.tintColor = .white
        cancel.addTarget(self, action: #selector(didTapCancel), for: .touchUpInside)
        cancel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(cancel)
        NSLayoutConstraint.activate([
            cancel.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -16),
            cancel.centerXAnchor.constraint(equalTo: view.centerXAnchor),
        ])
        startSession()
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        session?.stopRunning()
        session = nil
    }

    private func startSession() {
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device)
        else { return }
        let session = AVCaptureSession()
        guard session.canAddInput(input) else { return }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: DispatchQueue.main)
        output.metadataObjectTypes = [
            .qr, .ean13, .ean8, .code128, .code39, .pdf417, .aztec, .dataMatrix, .upce,
        ]
        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.layer.bounds
        view.layer.insertSublayer(preview, at: 0)
        session.startRunning()
        self.session = session
    }

    func metadataOutput(
        _ output: AVCaptureMetadataOutput,
        didOutput metadataObjects: [AVMetadataObject],
        from connection: AVCaptureConnection
    ) {
        guard !didReport,
              let object = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
              let value = object.stringValue, !value.isEmpty
        else { return }
        didReport = true
        session?.stopRunning()
        dismiss(animated: true) { [onScanned] in onScanned?(value) }
    }

    @objc private func didTapCancel() {
        session?.stopRunning()
        dismiss(animated: true)
    }
}

@available(iOS 17.0, *)
extension QRScanBridge {
    /// Presents the foreground scanner from the key window's root VC.
    /// Completion fires once with a broker-style payload, or with a
    /// PERMISSION_REQUIRED / ACTION_UNAVAILABLE payload without presenting.
    @MainActor
    static func present(completion: @escaping ([String: Any]) -> Void) {
        let status = AVCaptureDevice.authorizationStatus(for: .video)
        if status == .denied || status == .restricted {
            completion(["ok": false, "result": "PERMISSION_REQUIRED", "failure": "PERMISSION_REQUIRED"])
            return
        }
        if status == .notDetermined {
            Task { @MainActor in
                let granted = await AVCaptureDevice.requestAccess(for: .video)
                guard granted else {
                    completion(["ok": false, "result": "PERMISSION_REQUIRED", "failure": "PERMISSION_REQUIRED"])
                    return
                }
                presentScanner(completion: completion)
            }
            return
        }
        presentScanner(completion: completion)
    }

    @MainActor
    private static func presentScanner(completion: @escaping ([String: Any]) -> Void) {
        guard let presenter = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .flatMap({ $0.windows })
            .first(where: { $0.isKeyWindow })?
            .rootViewController
        else {
            completion(["ok": false, "result": "FAILED", "failure": "EXECUTION_FAILED"])
            return
        }
        let scanner = QRScannerViewController()
        scanner.onScanned = { raw in completion(QRScanBridge.payload(for: raw)) }
        presenter.present(scanner, animated: true)
    }
}
#endif
