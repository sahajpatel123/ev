import Darwin
import Foundation

/// launchd job for the always-on ears process.
///
/// WAKE FOUNDATION W1: ev.ears is the ONE always-on mic owner (launchd
/// KeepAlive+RunAtLoad true). EV.app surrenders the microphone while idle
/// and only acquires it for an accepted wake's Realtime handoff; it must
/// never fight ev.ears for the device. The old "kill on launch, dead on
/// quit" law is inverted.
enum EarsProcess {
    private static var domain: String { "gui/\(getuid())/ev.ears" }

    static func stop() {
        Task.detached(priority: .utility) {
            stopAndWait()
        }
    }

    /// Synchronous kill so live / Talk never start a second tap on a
    /// device `ev.ears` still holds (that abort looked like a quit).
    /// Retained for the brief Realtime handoff window only — idle EV.app
    /// must NOT call this (see ensureRunningForIdle).
    static func stopAndWait() {
        _ = runLaunchctl(["kill", "SIGKILL", domain])
    }

    /// Ensure the always-on listener is running (idle path local-only).
    /// Used when EV.app surrenders the mic or on quit so there is ONE
    /// mic owner. KeepAlive=true will restart after kill; this kickstart
    /// guarantees it is alive immediately without waiting for throttle.
    static func ensureRunning() {
        _ = runLaunchctl(["kickstart", "-k", domain])
    }

    /// Non-blocking variant for quit handlers.
    static func ensureRunningAsync() {
        Task.detached(priority: .utility) {
            ensureRunning()
        }
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
