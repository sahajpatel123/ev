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

    /// ONE mic owner handoff: while this marker names a live process, ears
    /// stands down and never opens the input. The owning PID makes a stale
    /// marker (crashed app) self-healing — ears ignores dead owners.
    enum LiveMicOwnerMarker {
        private static var url: URL {
            FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("EV", isDirectory: true)
                .appendingPathComponent("live-mic-owner")
        }

        static func set() {
            try? FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true
            )
            try? "\(ProcessInfo.processInfo.processIdentifier)"
                .write(to: url, atomically: true, encoding: .utf8)
        }

        static func clear() {
            try? FileManager.default.removeItem(at: url)
        }
    }

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
