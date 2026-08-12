import AppKit
import SwiftUI

/// EV — the SUIT menu-bar client.
///
/// Runs as an accessory (menu-bar-only) app. The ``AppModel`` owns the API
/// client, offline queue, hotkey, microphone capture, and notification
/// bridge; the SwiftUI menu-bar panel is a thin projection of its state.
struct EVApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView()
                .environmentObject(model)
                .frame(width: 380)
        } label: {
            Image(systemName: model.status.symbolName)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        // URL scheme delivery for EVNotificationHelper (single notification
        // path: backend → helper → ev:// → EV app → UNUserNotificationCenter).
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleAppleEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    @objc private func handleAppleEvent(_ event: NSAppleEventDescriptor, withReplyEvent replyEvent: NSAppleEventDescriptor) {
        guard let urlString = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              let url = URL(string: urlString) else {
            return
        }
        Task { @MainActor in
            NotificationBridge.shared.handle(url: url)
        }
    }
}

extension AppModel.Status {
    var symbolName: String {
        switch self {
        case .offline: return "circle.dashed"
        case .listening: return "ear"
        case .thinking: return "brain"
        case .speaking: return "waveform"
        }
    }

    var label: String {
        switch self {
        case .offline: return "offline"
        case .listening: return "listening"
        case .thinking: return "thinking"
        case .speaking: return "speaking"
        }
    }
}
