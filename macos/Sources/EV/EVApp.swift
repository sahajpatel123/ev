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
    /// EV's status item button, captured when a right-click hits the status
    /// bar so "Show/Hide EV Panel" can programmatically click it.
    private weak var statusButton: NSStatusBarButton?

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
        installAppMenu()
        installStatusItemMenu()
        // The always-on wake listener (ev.ears) is a companion to this app:
        // the microphone is only ever active while EV is open. Starting it
        // here ties its lifetime to the menu-bar app.
        startWakeListening()
    }

    /// Quitting must always terminate: no background task, chat stream, or
    /// consent prompt may hold the process open. The always-on wake listener
    /// is stopped here — before the app tears down — so the microphone is
    /// released immediately on quit (the ears process also self-checks as a
    /// safety net if the app is killed).
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        stopWakeListening()
        return .terminateNow
    }

    // MARK: - Always-on wake listener (ev.ears) lifecycle

    /// Start the ev.ears launchd job so the mic is listening while EV is open.
    private func startWakeListening() {
        Task.detached(priority: .utility) {
            Self.runLaunchctl(["kickstart", "-k", "gui/\(getuid())/ev.ears"])
        }
    }

    /// Stop the ev.ears launchd job so the mic is released when EV quits.
    ///
    /// A graceful SIGTERM is tried first, then SIGKILL after a beat: the ears
    /// process can be mid-request and ignore SIGTERM for a full HTTP timeout,
    /// but SIGKILL releases the microphone instantly (the OS reclaims the
    /// audio device on process death). `KeepAlive=false` on the job means
    /// launchd never restarts it.
    private func stopWakeListening() {
        let domain = "gui/\(getuid())/ev.ears"
        Self.runLaunchctl(["kill", "SIGTERM", domain])
        Thread.sleep(forTimeInterval: 1.0)
        Self.runLaunchctl(["kill", "SIGKILL", domain])
    }

    @discardableResult
    private static func runLaunchctl(_ arguments: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus
        } catch {
            // Best-effort: wake listening follows the app, never blocks it.
            return -1
        }
    }

    /// Menu-bar-only apps have no Dock icon or visible menu bar, so ⌘Q is the
    /// only keyboard quit path. Install an app menu with the standard Quit
    /// item (its key equivalent is processed whenever the panel is key).
    private func installAppMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenuItem.submenu = appMenu
        appMenu.addItem(
            withTitle: "Quit EV",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
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
        case .listening: return "Listening for EVIE"
        case .thinking: return "working on your question"
        case .speaking: return "speaking"
        }
    }
}
