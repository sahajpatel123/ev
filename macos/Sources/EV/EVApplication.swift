import AppKit
import EVRuntime

/// Custom `NSApplication` so last-window / stray `terminate` cannot kill
/// the accessory when SwiftUI has not wired `AppDelegate`.
@objc(EVApplication)
final class EVApplication: NSApplication {
    override func terminate(_ sender: Any?) {
        guard TerminatePolicy.allowsTerminate() else {
            return
        }
        super.terminate(sender)
    }
}
