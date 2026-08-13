import AppKit
import Foundation

/// Menu-bar (`LSUIElement`) apps stay out of the Dock and often never become
/// the active application. TCC permission sheets attach to the active app —
/// if EV is still an accessory, the sheet is created and immediately discarded,
/// and System Settings never lists EV for that service.
///
/// Bumping the activation policy to `.regular` for the duration of a grant
/// session is what makes the dialog appear *and* what writes the TCC row
/// that fills the Privacy pane. Nested calls share a depth count so the
/// Permissions window and an individual prompt can overlap without racing
/// the policy back to accessory mid-dialog.
@MainActor
enum AppForeground {
    private static var depth = 0

    static func begin() {
        depth += 1
        NSApp.setActivationPolicy(.regular)
        NSApp.unhide(nil)
        NSApp.activate()
        NSRunningApplication.current.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        for window in NSApp.windows where window.isVisible {
            window.makeKeyAndOrderFront(nil)
        }
    }

    static func end() {
        depth = max(0, depth - 1)
        if depth == 0 {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    /// Bring EV to the foreground, wait a beat so the policy flip sticks,
    /// run `work`, then restore accessory when the outermost caller finishes.
    static func withActivation<T: Sendable>(_ work: @MainActor () async -> T) async -> T {
        begin()
        // The first TCC sheet after a policy flip is dropped if it is created
        // in the same turn as `setActivationPolicy`.
        try? await Task.sleep(nanoseconds: 250_000_000)
        let result = await work()
        end()
        return result
    }
}
