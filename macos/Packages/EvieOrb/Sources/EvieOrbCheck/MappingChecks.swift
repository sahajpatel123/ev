import EvieOrb
import Foundation

/// Presence-event -> orb-state mapping table (+ locally derived offline).
func runMappingChecks(_ c: inout CheckContext) {
    c.expect(PresenceMapping.orbState(for: "turn.listening") == .listening, "listening map")
    c.expect(PresenceMapping.orbState(for: "turn.thinking") == .thinking, "thinking map")
    c.expect(PresenceMapping.orbState(for: "tool.started") == .acting, "acting map")
    c.expect(PresenceMapping.orbState(for: "turn.needs_confirm") == .needsConfirm, "needsConfirm map")
    c.expect(PresenceMapping.orbState(for: "turn.completed") == .success, "success map")
    c.expect(PresenceMapping.orbState(for: "turn.failed") == .error, "error map")
    c.expect(
        PresenceMapping.orbState(for: "panic.state", panicked: true) == .error,
        "panicked map"
    )
    c.expect(
        PresenceMapping.orbState(for: "panic.state", panicked: false) == .idle,
        "panic-cleared map"
    )
    c.expect(PresenceMapping.orbState(for: "turn.transcript.partial") == nil, "partial must be ignored")
    c.expect(PresenceMapping.orbState(for: "plan.updated") == nil, "plan.updated must be ignored")
    c.expect(PresenceMapping.orbState(for: "something.future") == nil, "future events must be ignored")
    c.expect(PresenceMapping.orbState(for: "") == nil, "empty event must be ignored")

    var detector = OfflineDetector(timeout: 20)
    detector.noteFrame(at: 100)
    c.expect(detector.isOffline(now: 110) == false, "online flagged offline")
    c.expect(detector.isOffline(now: 119.9) == false, "online flagged offline at edge")
    c.expect(detector.isOffline(now: 120.1) == true, "silence not flagged offline")
    detector.noteFrame(at: 130)
    c.expect(detector.isOffline(now: 131) == false, "did not recover after frame")
}
