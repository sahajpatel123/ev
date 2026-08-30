import Foundation
import WebKit
import EvieNativeBroker

final class NativeBridge: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    weak var webView: WKWebView?
    let broker = CapabilityBroker()

    static let bootstrapJS = """
    window.EvieNativeShell = {
      version: "\(BrokerVersion.version)",
      osVersion: (navigator && navigator.userAgent) || "",
      capabilities: ["contacts","alarms","timers","reminders","calendar","location","notifications","haptics","maps","phone_handoff","message_compose","facetime","app_launch_registry","share"],
      permissions: {},
      post: function (payload) {
        return new Promise(function (resolve) {
          var id = "n" + Date.now() + Math.random();
          window.__evieNativePending = window.__evieNativePending || {};
          window.__evieNativePending[id] = resolve;
          if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.evieNative) {
            window.webkit.messageHandlers.evieNative.postMessage(Object.assign({ request_id: id }, payload || {}));
          } else {
            resolve({ ok: false, failure: "NO_BRIDGE" });
          }
        });
      }
    };
    """

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "evieNative" else { return }
        guard let url = webView?.url, TrustedOrigin.allows(url) else { return }
        guard let body = message.body as? [String: Any],
              let request = NativeBridgeRequest.parse(body) else { return }
        Task { @MainActor in
            let reply = await broker.handle(request, raw: body)
            let requestID = body["request_id"] as? String ?? ""
            let json = "{}"
            if let data = try? JSONSerialization.data(withJSONObject: reply),
               let text = String(data: data, encoding: .utf8) {
                json = text
            }
            let js = "window.__evieNativePending && window.__evieNativePending['\(requestID)'] && window.__evieNativePending['\(requestID)'](\(json)); delete window.__evieNativePending['\(requestID)'];"
            webView?.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, !TrustedOrigin.allows(url), navigationAction.targetFrame?.isMainFrame == true {
            webView.configuration.userContentController.removeScriptMessageHandler(forName: "evieNative")
        }
        decisionHandler(.allow)
    }
}
