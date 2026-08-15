import Darwin
import Foundation

/// launchd job for the always-on ears process.
///
/// While EV.app is open the menu-bar client owns the microphone for the
/// full-duplex live channel. Starting `ev.ears` at the same time fights
/// for the same input device, so the app stops that job on launch and
/// does not restart it on quit.
enum EarsProcess {
    private static var domain: String { "gui/\(getuid())/ev.ears" }

    static func stop() {
        Task.detached(priority: .utility) {
            stopAndWait()
        }
    }

    /// Synchronous kill so live / Talk never start a second tap on a
    /// device `ev.ears` still holds (that abort looked like a quit).
    static func stopAndWait() {
        _ = runLaunchctl(["kill", "SIGKILL", domain])
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
            return -1
        }
    }
}
