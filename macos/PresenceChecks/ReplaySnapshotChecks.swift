import AppKit
import Foundation
import PresenceCore
import SwiftUI

/// D6 replay snapshots: script end-states rendered through the FULL pipeline
/// (script → transport → store → view). Same pixels the live stream would
/// produce for the same frames — the store cannot tell replay from live.
@MainActor
enum ReplaySnapshotChecks {
    static func drain(_ steps: [FixtureTransport.Step]) async throws -> PresenceSnapshot {
        var snapshot = PresenceSnapshot()
        for try await item in FixtureTransport(steps: steps).run() {
            snapshot.apply(item)
        }
        return snapshot
    }

    static func run(_ check: CheckFn) async {
        do {
            let six = try await drain(PresenceScripts.sixStepTask(paceMs: 0))
            let confirmSnap = try await drain(PresenceScripts.sendNeedingConfirm(paceMs: 0))
            let fail = try await drain(PresenceScripts.failureWithRecovery(paceMs: 0))
            // Failure mid-state: drain only the failing turn's frames.
            var midFail = PresenceSnapshot()
            var n = 0
            for try await item in FixtureTransport(
                steps: PresenceScripts.failureWithRecovery(paceMs: 0)
            ).run() {
                midFail.apply(item)
                n += 1
                if n == 7 { break } // through turn.failed
            }
            let panicFull = try await drain(PresenceScripts.panic(paceMs: 0))
            var midPanic = PresenceSnapshot()
            var m = 0
            for try await item in FixtureTransport(steps: PresenceScripts.panic(paceMs: 0)).run() {
                midPanic.apply(item)
                m += 1
                if m == 5 { break } // connection + hello + started + thinking + panic engaged
            }
            check(
                "replay-snap-states",
                six.pill != .idle && confirmSnap.turn?.confirm != nil
                    && midPanic.panic.panicked && panicFull.panic.panicked == false,
                "\(six.pill) \(midPanic.panic.panicked)"
            )
            let pillStates: [(String, PresenceSnapshot, Bool)] = [
                ("replay-sixstep", six, true),
                ("replay-failed", midFail, false),
                ("replay-panic", midPanic, true),
            ]
            for scheme: ColorScheme in [.light, .dark] {
                let tag = scheme == .dark ? "dark" : "light"
                for (name, snap, expanded) in pillStates {
                    let size = NSSize(
                        width: 440, height: expanded ? 320 : 76
                    )
                    let url = SnapshotRender.render(name: "\(name)-\(tag)", size: size, scheme: scheme) {
                        HUDPillView(snapshot: snap, expanded: expanded)
                    }
                    if url == nil {
                        check("snap-\(name)-\(tag)", false, "render failed")
                    } else {
                        SnapshotRender.checkPNG(name, scheme: tag, expectedSize: size, check: check)
                    }
                }
            }
            // The money shot: the confirm card for a REPLAYED ticket.
            if let ticket = confirmSnap.turn?.confirm {
                let model = ConfirmCardModel(ticket: ticket, client: FakeConfirmClient())
                // Pin the clock near the script's timestamps so the
                // countdown reads deterministically ("Expires in 1:11").
                if let pinned = ISO8601DateFormatter().date(from: "2026-10-26T19:03:00Z") {
                    model.setNow(pinned)
                }
                for scheme: ColorScheme in [.light, .dark] {
                    let tag = scheme == .dark ? "dark" : "light"
                    let url = SnapshotRender.render(
                        name: "replay-card-\(tag)",
                        size: NSSize(width: 480, height: 560), scheme: scheme
                    ) {
                        ConfirmCardView(model: model)
                    }
                    if url == nil {
                        check("snap-replay-card-\(tag)", false, "render failed")
                    } else {
                        SnapshotRender.checkPNG(
                            "replay-card", scheme: tag,
                            expectedSize: NSSize(width: 480, height: 560), check: check
                        )
                    }
                }
            } else {
                check("snap-replay-card", false, "no replayed ticket")
            }
            _ = fail
        } catch {
            check("replay-snap-states", false, "\(error)")
        }
    }
}
