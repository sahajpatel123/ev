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

        // ---- STOP MUST NEVER SURFACE AS AN ERROR ----
        // Regression: clicking Stop Speaking painted "CancellationError()"
        // in the panel. The stop intentionally cancels in-flight speech
        // work; every user-visible error path on that surface must treat
        // cancellation as a quiet success.
        let stopRegion = region(live, from: "func stopAssistantSpeech()", to: "model.status = .listening")
        check(
            "intv-stop-clears-error",
            stopRegion?.contains("model.lastError = nil") == true,
            "a successful Stop clears the error banner instead of showing the cancellation"
        )
        check(
            "intv-tts-download-cancellation-silent",
            live.contains("TTS download failed")
                && live.contains("error is CancellationError"),
            "a TTS fetch cancelled by Stop must not set a download-failed error"
        )
        check(
            "intv-socket-cancel-quiet",
            live.contains("(error as? URLError)?.code == .cancelled"),
            "a cancelled live-socket read exits quietly, not as a disconnect error"
        )
        let appModel = source("AppModel.swift")
        let chatRegion = region(appModel, from: "func sendChat", to: "MARK: - Hands-free")
        check(
            "intv-chat-stream-cancellation-silent",
            chatRegion?.contains("catch is CancellationError") == true,
            "a chat stream cancelled by Stop ends the turn without an error banner"
        )
        let followModel = source("FollowMeModel.swift")
        let followView = source("FollowMeView.swift")
        check(
            "intv-panel-never-renders-cancellation-type",
            !followModel.contains("String(describing: error)")
                && !followView.contains("String(describing: error)")
                && followModel.contains("isCancellation"),
            "menu-panel errors must never render the raw CancellationError() type name"
        )
    }

    /// Source slice from `from` through the end of `to` (nil when absent).
    static func region(_ text: String, from: String, to: String) -> String? {
        guard let start = text.range(of: from) else { return nil }
        let tail = text[start.lowerBound...]
        guard let end = tail.range(of: to) else { return nil }
        return String(tail[..<end.upperBound])
    }
}
