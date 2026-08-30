import Foundation

/// INTERRUPTION V1 composition checks (project directive 2026-08-23):
/// feature OFF = architecturally absent. Invoked from EVMicTalkTests.main.
enum InterruptV1PolicyChecks {
    static func source(_ name: String) -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/EV/\(name)")
        return (try? String(contentsOf: url, encoding: .utf8)) ?? ""
    }

    static func run(_ check: (String, Bool, String) -> Void) {
        let live = source("LiveConversation.swift")
        let monitor = source("ExplicitInterruptMonitor.swift")

        // ---- CLOSURE LAW (2026-08-23): spoken interruption is REMOVED ----
        check(
            "intv-closed-no-monitor-construction",
            !live.contains("ExplicitInterruptMonitor(controlQueue")
                && !live.contains("interruptMonitor = ExplicitInterruptMonitor"),
            "production must never construct the spoken-interruption monitor"
        )
        check(
            "intv-closed-detector-marked-dead",
            monitor.contains("DEAD / LEGACY / UNWIRED"),
            "legacy detector source carries the closure marker"
        )
        check(
            "intv-deterministic-stop-present",
            live.contains("func stopAssistantSpeech()")
                && live.contains("ui_stop"),
            "deterministic Stop uses the proven local-stop-first executor"
        )
        check(
            "intv-stop-idempotent-when-idle",
            live.contains("guard model.status == .speaking || model.player.isPlaying else { return }"),
            "Stop with nothing playing must be a no-op"
        )
    }
}
