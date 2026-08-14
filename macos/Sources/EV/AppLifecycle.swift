import AppKit

/// The single quit path for the menu-bar accessory app.
///
/// EV is an `LSUIElement` app: no Dock icon and no visible menu bar, so the
/// usual macOS quit routes (Dock right-click, ⌘Q) are not available by
/// default. Every quit control — the footer button, the Permissions panel
/// button, the ⌘Q app-menu item, and the status-item context menu — funnels
/// through here so the app always terminates cleanly and predictably.
@MainActor
enum AppLifecycle {
    static func quit() {
        // `applicationShouldTerminate` returns `.terminateNow`, so this always
        // ends the process even if a background task is mid-flight.
        NSApp.terminate(nil)
    }
}
