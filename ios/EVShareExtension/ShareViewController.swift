import EVClient
import UIKit
import UniformTypeIdentifiers

/// Share extension: text, URLs, and files become EV captures/attachments.
///
/// Uses the same Keychain service as the main app (shared keychain-access
/// group), so the token written by the app is available here without a second
/// login flow.
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let label = UILabel()
        label.text = "Adding to EV…"
        label.textAlignment = .center
        label.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])

        collectAndSend()
    }

    private func collectAndSend() {
        guard let items = extensionContext?.inputItems as? [NSExtensionItem] else {
            complete()
            return
        }

        var texts: [String] = []
        var files: [URL] = []
        let group = DispatchGroup()

        for item in items {
            guard let providers = item.attachments else { continue }
            for provider in providers {
                if provider.hasItemConformingToTypeIdentifier(UTType.text.identifier) {
                    group.enter()
                    provider.loadItem(forTypeIdentifier: UTType.text.identifier, options: nil) { result, _ in
                        if let text = result as? String {
                            texts.append(text)
                        }
                        group.leave()
                    }
                } else if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
                    group.enter()
                    provider.loadItem(forTypeIdentifier: UTType.url.identifier, options: nil) { result, _ in
                        if let url = result as? URL {
                            texts.append(url.absoluteString)
                        }
                        group.leave()
                    }
                } else if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                    group.enter()
                    provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { result, _ in
                        if let url = result as? URL {
                            files.append(url)
                        }
                        group.leave()
                    }
                }
            }
        }

        group.notify(queue: .main) {
            Task { await self.send(texts: texts, files: files) }
        }
    }

    private func send(texts: [String], files: [URL]) async {
        let config = EVClientAppConfig()
        let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        var errorMessage: String?

        let text = texts.joined(separator: "\n")
        if !text.isEmpty {
            do {
                _ = try await client.capture(
                    payload: CapturePayload(text: text, deviceID: config.deviceID)
                )
            } catch {
                errorMessage = "Capture failed: \(error)"
            }
        }

        for url in files {
            guard let data = try? Data(contentsOf: url) else { continue }
            do {
                _ = try await client.attach(
                    filename: url.lastPathComponent,
                    contentType: "application/octet-stream",
                    data: data,
                    source: "ios-share"
                )
            } catch {
                errorMessage = "Attachment failed: \(error)"
            }
        }

        if let errorMessage {
            let alert = UIAlertController(title: "EV", message: errorMessage, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in
                self.complete()
            })
            present(alert, animated: true)
        } else {
            complete()
        }
    }

    private func complete() {
        extensionContext?.completeRequest(returningItems: nil) { _ in }
    }
}
