import AppKit
import EVRuntime
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
    /// EV's status item button, captured when a right-click hits the status
    /// bar so "Show/Hide EV Panel" can programmatically click it.
    private weak var statusButton: NSStatusBarButton?
    /// Never-shown window so closing the menu panel / TCC dialog is not
    /// “last window closed” even if SwiftUI skips this delegate.
    private var keepAliveWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installKeepAliveWindow()
        // URL scheme delivery for EVNotificationHelper (single notification
        // path: backend → helper → ev:// → EV app → UNUserNotificationCenter).
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleAppleEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
        installAppMenu()
        installStatusItemMenu()
        // In-app live owns the microphone. ev.ears is a wake-word front end
        // and must not share the same input device while EV.app is open.
        EarsProcess.stopAndWait()
    }

    /// The window-style menu panel is a real `NSWindow`. Closing it (Talk,
    /// a mic prompt, clicking away) used to look like “last window closed”
    /// and quit EV mid-conversation. Only an explicit Quit may terminate.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        TerminatePolicy.shouldTerminateAfterLastWindowClosed
    }

    /// Voice / live errors must not terminate. Explicit Quit sets
    /// ``TerminatePolicy.explicitQuit`` first.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let reply = TerminatePolicy.reply()
        if reply == .terminateNow {
            EarsProcess.stopAndWait()
        }
        return reply
    }

    private func installKeepAliveWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1, height: 1),
            styleMask: .borderless,
            backing: .buffered,
            defer: true
        )
        window.isReleasedWhenClosed = false
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.transient, .ignoresCycle]
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
        window.orderFrontRegardless()
        keepAliveWindow = window
    }

    /// Menu-bar-only apps have no Dock icon or visible menu bar, so ⌘Q is the
    /// only keyboard quit path. Install an app menu with the standard Quit
    /// item (its key equivalent is processed whenever the panel is key).
    private func installAppMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenuItem.submenu = appMenu
        let quitItem = appMenu.addItem(
            withTitle: "Quit EV",
            action: #selector(quitFromMenu),
            keyEquivalent: "q"
        )
        quitItem.target = self
        mainMenu.addItem(appMenuItem)
        NSApp.mainMenu = mainMenu
    }

    /// The window-style MenuBarExtra has no right-click menu, so intercept
    /// right-clicks on EV's status-bar window (the app owns exactly one
    /// status item) and offer Quit there too.
    private func installStatusItemMenu() {
        NSEvent.addLocalMonitorForEvents(matching: [.rightMouseUp]) { [weak self] event in
            guard let self, let window = event.window,
                  window.className.contains("StatusBar") else {
                return event
            }
            self.statusButton = window.contentView?.firstStatusButton()
            self.showStatusMenu(at: event.locationInWindow, in: window)
            return nil
        }
    }

    private func showStatusMenu(at point: NSPoint, in window: NSWindow) {
        let menu = NSMenu()
        let openItem = NSMenuItem(
            title: "Show/Hide EV Panel",
            action: #selector(togglePanel),
            keyEquivalent: ""
        )
        openItem.target = self
        menu.addItem(openItem)
        menu.addItem(.separator())
        let quitItem = NSMenuItem(
            title: "Quit EV",
            action: #selector(quitFromMenu),
            keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)
        if let view = window.contentView {
            menu.popUp(positioning: nil, at: point, in: view)
        }
    }

    @objc private func togglePanel() {
        // Click the status button, which triggers the MenuBarExtra panel
        // toggle. Fall back to activating the app if the button is gone.
        if let statusButton {
            statusButton.performClick(nil)
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    @MainActor @objc private func quitFromMenu() {
        AppLifecycle.quit()
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

private extension NSView {
    /// Finds the first status-item button in this view's subtree.
    func firstStatusButton() -> NSStatusBarButton? {
        if let button = self as? NSStatusBarButton {
            return button
        }
        for subview in subviews {
            if let found = subview.firstStatusButton() {
                return found
            }
        }
        return nil
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
        case .thinking: return "working on your question"
        case .speaking: return "speaking"
        }
    }
}
