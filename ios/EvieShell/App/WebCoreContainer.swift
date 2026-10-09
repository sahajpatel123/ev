import SwiftUI
import WebKit
import EvieNativeBroker

struct WebCoreContainer: UIViewRepresentable {
    func makeCoordinator() -> NativeBridge {
        NativeBridge()
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        let user = WKUserContentController()
        user.add(context.coordinator, name: "evieNative")
        user.addUserScript(WKUserScript(source: NativeBridge.bootstrapJS, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController = user
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        context.coordinator.webView = view
        view.load(URLRequest(url: AppOrigin.homeURL))
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

enum AppOrigin {
    /// Runtime override so both iPhones can point at the Mac without a rebuild:
    /// `defaults write com.ev.evie.shell evie.api_origin -string "https://<mac>.ts.net"`
    /// or set via the shell Settings screen when present. Bundle EV_API_URL
    /// stays the simulator default (http://127.0.0.1:8000). Voice requires the
    /// https ts.net value — http://100.x loads diagnostics but mic stays blocked.
    static var apiOrigin: String {
        if let raw = UserDefaults.standard.string(forKey: "evie.api_origin"),
           !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return normalized(raw)
        }
        if let raw = Bundle.main.object(forInfoDictionaryKey: "EV_API_URL") as? String, !raw.isEmpty {
            return normalized(raw)
        }
        return "http://127.0.0.1:8000"
    }

    static func normalized(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s = String(s.dropLast()) }
        return s.isEmpty ? "http://127.0.0.1:8000" : s
    }

    static var homeURL: URL {
        URL(string: apiOrigin + "/evie/")
            ?? URL(string: "http://127.0.0.1:8000/evie/")!
    }

    /// True when this URL can reach the owner's Home Station (not the phone's
    /// own loopback, not a placeholder).
    static func isHomeStationOrigin(_ raw: String) -> Bool {
        let value = normalized(raw)
        guard
            let url = URL(string: value),
            let scheme = url.scheme?.lowercased(),
            let host = url.host?.lowercased(),
            !host.isEmpty
        else { return false }
        if host == "127.0.0.1" || host == "localhost" || host == "::1" { return false }
        if value == "http://127.0.0.1:8000" { return false }
        return scheme == "https" || host.hasSuffix(".ts.net")
    }

    /// First-run gate: the shell must know where the Home Station is before
    /// the web core can load. A release IPA carries EV_API_URL in its
    /// Info.plist; a direct Xcode install starts at loopback and needs the
    /// owner to enter the Tailscale HTTPS address once.
    static func needsSetup(_ stored: String) -> Bool {
        let trimmed = stored.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return !isHomeStationOrigin(trimmed) }
        if let raw = Bundle.main.object(forInfoDictionaryKey: "EV_API_URL") as? String,
           isHomeStationOrigin(raw) {
            return false
        }
        return true
    }
}

// Cycle 32 — iPhone-only, backward compat: offline-retry policy for the web-core loader.
// Pure value type; WebCoreContainer behavior unchanged.
struct EvieOfflineRetryPolicy: Sendable, Equatable {
    var maxAttempts: Int = 5
    var baseDelaySeconds: Double = 1.0
    var maxDelaySeconds: Double = 30.0

    func delay(forAttempt attempt: Int) -> Double {
        guard attempt > 0 else { return 0 }
        let shift = min(max(attempt - 1, 0), 10)
        let delay = baseDelaySeconds * Double(1 << shift)
        return min(delay, maxDelaySeconds)
    }
}
