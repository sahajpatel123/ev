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
        context.coordinator.webView = view
        view.load(URLRequest(url: AppOrigin.homeURL))
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

enum AppOrigin {
    static var apiOrigin: String {
        if let raw = Bundle.main.object(forInfoDictionaryKey: "EV_API_URL") as? String, !raw.isEmpty {
            return raw.hasSuffix("/") ? String(raw.dropLast()) : raw
        }
        return "http://127.0.0.1:8000"
    }

    static var homeURL: URL {
        URL(string: apiOrigin + "/evie/")!
    }
}
