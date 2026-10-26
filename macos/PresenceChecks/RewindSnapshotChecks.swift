import AppKit
import Foundation
import PresenceCore
import SwiftUI

/// D5 snapshot suite: timeline states → offscreen PNG (light + dark).
/// Models refresh through the fake transport first, so the rows rendered
/// are ones the real pipeline produces.
@MainActor
enum RewindSnapshotChecks {
    static let size = NSSize(width: 380, height: 560)
    static let now = Date(timeIntervalSince1970: 1_792_000_000)

    static func populated() async -> RewindTimelineModel {
        let sample = RewindModelChecks.sample()
        let fake = FakeTimelineClient(actions: sample.actions, entries: sample.entries)
        let model = RewindTimelineModel(client: fake, now: { now })
        await model.refresh()
        return model
    }

    static func run(_ check: CheckFn) async {
        let today = await populated()
        let restored = await populated()
        let rows = restored.rows
        if let target = rows.first(where: { $0.undoId != nil }) {
            await restored.restore(target)
        }
        let empty = RewindTimelineModel(client: FakeTimelineClient(), now: { now })
        await empty.refresh()
        let failed = RewindTimelineModel(
            client: FakeTimelineClient(fetchError: .requestFailed(
                message: "Couldn't reach the backend."
            )),
            now: { now }
        )
        await failed.refresh()
        let states: [(String, RewindTimelineModel)] = [
            ("timeline-today", today),
            ("timeline-restored", restored),
            ("timeline-empty", empty),
            ("timeline-error", failed),
        ]
        check("timeline-snapshot-rows", today.rows.count == 3, "\(today.rows.count)")
        for scheme: ColorScheme in [.light, .dark] {
            let tag = scheme == .dark ? "dark" : "light"
            for (name, model) in states {
                // Menu-panel backdrop: the live host (MenuBarView) provides
                // the window background; snapshots paint the same color so
                // text contrast matches what the owner will see.
                let url = SnapshotRender.render(name: "\(name)-\(tag)", size: size, scheme: scheme) {
                    RewindTimelineView(model: model, onBack: {})
                        .padding(12)
                        .background(Color(nsColor: .windowBackgroundColor))
                }
                if url == nil {
                    check("snap-\(name)-\(tag)", false, "render failed")
                } else {
                    SnapshotRender.checkPNG(name, scheme: tag, expectedSize: size, check: check)
                }
            }
        }
    }
}
