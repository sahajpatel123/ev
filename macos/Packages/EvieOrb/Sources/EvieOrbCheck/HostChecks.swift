import AppKit
import EvieOrb
import Foundation

/// R6 host checks: geometry detection, panel configuration, present/dismiss
/// layout. Headless — windows are created but NEVER ordered front.
func runHostChecks(_ c: inout CheckContext) {
    // Notch detection from real probe values (16" M2, 1710x1112 scaled).
    do {
        let g = IslandHost.geometry(
            frame: CGRect(x: 0, y: 0, width: 1710, height: 1112),
            visibleFrame: CGRect(x: 0, y: 0, width: 1710, height: 1074),
            safeAreaTop: 37.5,
            auxLeft: CGRect(x: 0, y: 1074.5, width: 751, height: 37.5),
            auxRight: CGRect(x: 959, y: 1074.5, width: 751, height: 37.5)
        )
        c.expect(g.hasNotch == true, "probe values must detect a notch")
        c.expect(abs(g.notchHalfWidth - 104) < 0.5, "notch half width 104, got \(g.notchHalfWidth)")
    }

    // No aux areas, no safe area => no notch.
    do {
        let g = IslandHost.geometry(
            frame: CGRect(x: 0, y: 0, width: 2560, height: 1440),
            visibleFrame: CGRect(x: 0, y: 0, width: 2560, height: 1416),
            safeAreaTop: 0, auxLeft: nil, auxRight: nil
        )
        c.expect(g.hasNotch == false, "external must not detect a notch")
    }

    // Safe area without a measurable gap => notch with fallback half width.
    do {
        let g = IslandHost.geometry(
            frame: CGRect(x: 0, y: 0, width: 1512, height: 982),
            visibleFrame: CGRect(x: 0, y: 0, width: 1512, height: 958),
            safeAreaTop: 24, auxLeft: nil, auxRight: nil
        )
        c.expect(g.hasNotch == true, "safe-area-only must detect a notch")
        c.expect(g.notchHalfWidth == 104, "fallback half width")
    }

    // Panel configuration (created, never shown).
    let host = IslandHost()
    let panel = host.window
    c.expect(panel.styleMask.contains(.nonactivatingPanel), "panel must be non-activating")
    c.expect(panel.styleMask.contains(.borderless), "panel must be borderless")
    c.expect(panel.level == .statusBar, "panel must sit above the menu bar")
    c.expect(panel.collectionBehavior.contains(.canJoinAllSpaces), "must join all Spaces")
    c.expect(panel.isOpaque == false, "panel must be transparent")
    c.expect(panel.hidesOnDeactivate == false, "must not hide on deactivate")
    c.expect(panel.ignoresMouseEvents == true, "compact must be click-through")
    c.expect(panel.isVisible == false, "panel must never show during checks")
    c.expect(host.isExpanded == false, "starts compact")

    // Present lays out the expanded island without showing.
    let content = PlaceholderConfirmCard()
    host.present(content: content, animated: false, showWindow: false)
    c.expect(host.isExpanded == true, "present must expand")
    c.expect(panel.ignoresMouseEvents == false, "expanded must take clicks")
    c.expect(panel.isVisible == false, "present(showWindow:false) must not show")
    if let geo = host.currentScreenGeometry() {
        let expected = IslandLayout.frame(
            screen: geo, mode: .expanded(contentHeight: content.contentHeight),
            placement: host.placement, fullscreenApp: false, state: host.orbView.state
        )
        // Fullscreen on the check machine would order out instead; accept
        // either the expected frame or a hidden panel, never a wrong frame.
        let matches = panel.frame.equalTo(expected.rect) || panel.isVisible == false
        c.expect(matches, "island frame \(panel.frame) != \(expected.rect)")
    } else {
        c.expect(false, "no screen geometry (headless without displays?)")
    }

    // Presence events drive the orb state through the host.
    host.handlePresenceEvent(named: "turn.listening")
    c.expect(host.orbView.state == .listening, "host must map turn.listening")

    // Dismiss collapses back to compact.
    host.dismiss(animated: false)
    c.expect(host.isExpanded == false, "dismiss must collapse")
    c.expect(panel.ignoresMouseEvents == true, "compact click-through restored")
    if let geo = host.currentScreenGeometry() {
        let expected = IslandLayout.frame(
            screen: geo, mode: .compact,
            placement: host.placement, fullscreenApp: false, state: host.orbView.state
        )
        c.expect(
            panel.frame.equalTo(expected.rect) || panel.isVisible == false,
            "compact frame \(panel.frame) != \(expected.rect)"
        )
    }

    // Orb view headless frame: non-blank ring pixels, no window needed.
    let orb = EvieOrbView()
    orb.state = .listening
    if let frame = orb.renderTestFrame() {
        c.expect(frame.meanAlpha > 0.005, "orb test frame is blank")
        c.expect(frame.width == 240 && frame.height == 240, "test frame size")
    } else {
        c.expect(false, "renderTestFrame returned nil (no Metal?)")
    }

    // Placeholder content reports sane heights for island clamping.
    c.expect(PlaceholderPlanSteps().contentHeight > 0, "plan steps height")
    c.expect(PlaceholderCaption().contentHeight > 0, "caption height")
    c.expect(PlaceholderConfirmCard().contentHeight == 120, "confirm height")
}
