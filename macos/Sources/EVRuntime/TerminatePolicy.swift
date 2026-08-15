import AppKit

/// Shipped terminate policy for the LSUIElement menu-bar client.
///
/// Closing the panel (Talk, a mic consent dialog, click-away) is not a
/// quit. SwiftUI `MenuBarExtra` can skip an unwired `NSApplicationDelegate`,
/// so `EVApplication.terminate` and the delegate both call through here.
public enum TerminatePolicy {
    /// Set only by the explicit Quit controls.
    public static var explicitQuit = false

    public static var shouldTerminateAfterLastWindowClosed: Bool { false }

    public static func markExplicitQuit() {
        explicitQuit = true
    }

    public static func allowsTerminate(explicitQuit: Bool = explicitQuit) -> Bool {
        explicitQuit
    }

    public static func reply(
        explicitQuit: Bool = explicitQuit
    ) -> NSApplication.TerminateReply {
        allowsTerminate(explicitQuit: explicitQuit) ? .terminateNow : .terminateCancel
    }
}
