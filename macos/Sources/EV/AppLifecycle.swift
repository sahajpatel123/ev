import AppKit
import EVRuntime

/// The single quit path for the menu-bar accessory app.
///
/// EV is an `LSUIElement` app: no Dock icon and no visible menu bar, so the
/// usual macOS quit routes (Dock right-click, ⌘Q) are not available by
/// default. Every quit control — the footer button, the Permissions panel
/// button, the ⌘Q app-menu item, and the status-item context menu — funnels
/// through here so the app always terminates cleanly and predictably.
enum AppLifecycle {
    /// Set only by the explicit Quit controls (footer, ⌘Q, status menu).
    /// Voice errors, mute, barge-in, and the last panel window closing
    /// must never flip this — those used to terminate EV.app mid-sentence.
    static var isQuitting: Bool {
        get { TerminatePolicy.explicitQuit }
        set { TerminatePolicy.explicitQuit = newValue }
    }

    @MainActor
    static func quit() {
        VoiceOrbOverlay.shared.hide()
        TerminatePolicy.markExplicitQuit()
        NSApp.terminate(nil)
    }
}
