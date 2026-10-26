import AppKit
import Foundation
import PresenceCore
import PresenceGesture

/// Owner-run guided checks (D7). Code only until the owner runs it:
/// `swift run --package-path macos presence-check <flow>`.
///
/// Flows: `calibrate` (60 s gesture timing + false-trigger proof),
/// `permissions` (prompt-free Input Monitoring gate),
/// `hud` (visible pill cycled through live states + owner confirmation).
/// Exit 0 = PASS, 1 = FAIL, 2 = cannot run (missing grant), 3 = usage.
@main
enum PresenceCheck {
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        let json = args.contains("--json")
        let flow = args.first { !$0.hasPrefix("-") } ?? ""
        let code: Int32
        switch flow {
        case "calibrate":
            code = CalibrateFlow.run(json: json)
        case "permissions":
            code = PermissionsFlow.run()
        case "hud":
            code = HudFlow.run()
        default:
            print("usage: presence-check [--json] <calibrate|permissions|hud>")
            print("  calibrate    60-second Option-key timing + typing false-trigger proof")
            print("  permissions  prompt-free Input Monitoring / Accessibility gate")
            print("  hud          cycle the real pill through live states (owner confirms)")
            code = 3
        }
        exit(code)
    }

    static func waitEnter(_ prompt: String) {
        print(prompt, terminator: "")
        _ = readLine()
    }

    /// Run the main runloop in slices until `done()` or `timeout` seconds.
    /// Callbacks (tap, timers) fire while the CLI waits.
    static func runUntil(timeout: TimeInterval, done: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline, !done() {
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        }
    }
}
