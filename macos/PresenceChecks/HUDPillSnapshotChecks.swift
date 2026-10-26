import AppKit
import Foundation
import PresenceCore
import SwiftUI

/// D3 snapshot suite: every HUD pill state → offscreen PNG (light + dark),
/// plus increased-contrast and reduced-motion variants. No windows.
@MainActor
enum HUDPillSnapshotChecks {
    static let compact = NSSize(width: 440, height: 76)
    static let expanded = NSSize(width: 440, height: 320)
    // NOTE: run() is async so the runner's hop is a real suspension point.

    static func pill(
        _ snapshot: PresenceSnapshot, expanded: Bool,
        contrast: ColorSchemeContrast? = nil
    ) -> HUDPillView {
        HUDPillView(
            snapshot: snapshot, captionsEnabled: true, expanded: expanded,
            onToggleExpanded: {}, onReviewConfirm: {}, onDismiss: {},
            contrastOverride: contrast
        )
    }

    static func run(_ check: CheckFn) async {
        // The matrix must actually cover every pill state it names.
        check("fixture-listening", PresenceFixtures.listening().pill == .listening(level: 0.6, degraded: false))
        check("fixture-thinking", PresenceFixtures.thinking().pill == .thinking(label: "Thinking…"))
        if case .acting = PresenceFixtures.acting().pill {
            check("fixture-acting", true)
        } else {
            check("fixture-acting", false, "\(PresenceFixtures.acting().pill)")
        }
        if case .needsConfirm = PresenceFixtures.needsConfirm().pill {
            check("fixture-needs-confirm", true)
        } else {
            check("fixture-needs-confirm", false, "\(PresenceFixtures.needsConfirm().pill)")
        }
        if case .speaking = PresenceFixtures.speaking().pill {
            check("fixture-speaking", true)
        } else {
            check("fixture-speaking", false, "\(PresenceFixtures.speaking().pill)")
        }
        if case .completed = PresenceFixtures.completed().pill {
            check("fixture-completed", true)
        } else {
            check("fixture-completed", false, "\(PresenceFixtures.completed().pill)")
        }
        if case .failed = PresenceFixtures.failed().pill {
            check("fixture-failed", true)
        } else {
            check("fixture-failed", false, "\(PresenceFixtures.failed().pill)")
        }
        let compactStates: [(String, PresenceSnapshot)] = [
            ("pill-listening", PresenceFixtures.listening()),
            ("pill-listening-degraded", PresenceFixtures.listening(level: 0, degraded: true)),
            ("pill-thinking", PresenceFixtures.thinking()),
            ("pill-acting", PresenceFixtures.acting()),
            ("pill-needs-confirm", PresenceFixtures.needsConfirm()),
            ("pill-speaking", PresenceFixtures.speaking()),
            ("pill-completed", PresenceFixtures.completed()),
            ("pill-failed", PresenceFixtures.failed()),
        ]
        let expandedStates: [(String, PresenceSnapshot)] = [
            ("pill-acting-expanded", PresenceFixtures.acting()),
            ("pill-panic", PresenceFixtures.panicked()),
            ("pill-reconnecting", PresenceFixtures.reconnecting()),
        ]
        for scheme: ColorScheme in [.light, .dark] {
            let tag = scheme == .dark ? "dark" : "light"
            for (name, snap) in compactStates {
                let url = SnapshotRender.render(
                    name: "\(name)-\(tag)", size: compact, scheme: scheme
                ) { pill(snap, expanded: false) }
                if url == nil {
                    check("snap-\(name)-\(tag)", false, "render failed")
                } else {
                    SnapshotRender.checkPNG(name, scheme: tag, expectedSize: compact, check: check)
                }
            }
            for (name, snap) in expandedStates {
                let url = SnapshotRender.render(
                    name: "\(name)-\(tag)", size: expanded, scheme: scheme
                ) { pill(snap, expanded: true) }
                if url == nil {
                    check("snap-\(name)-\(tag)", false, "render failed")
                } else {
                    SnapshotRender.checkPNG(name, scheme: tag, expectedSize: expanded, check: check)
                }
            }
        }
        // Access variant: increased contrast (acting, expanded). There is no
        // reduced-motion variant: presence views are fully static (all
        // motion is CLI 3's orb), so Reduce Motion holds by construction.
        let acting = PresenceFixtures.acting()
        let contrastURL = SnapshotRender.render(
            name: "pill-acting-contrast", size: expanded, scheme: .light
        ) {
            pill(acting, expanded: true, contrast: .increased)
        }
        if contrastURL == nil {
            check("snap-pill-acting-contrast", false, "render failed")
        } else {
            SnapshotRender.checkPNG("pill-acting", scheme: "contrast", expectedSize: expanded, check: check)
        }
    }
}
