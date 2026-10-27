import EvieOrb
import Foundation

/// R6: island layout with simulated screen geometries (no AppKit windows).
func runLayoutChecks(_ c: inout CheckContext) {
    // Simulated screens (AppKit coords, origin bottom-left).
    let notch14 = ScreenGeometry(
        frame: CGRect(x: 0, y: 0, width: 1512, height: 982),
        visibleFrame: CGRect(x: 0, y: 0, width: 1512, height: 958),
        hasNotch: true, notchHalfWidth: 100
    )
    let notch16 = ScreenGeometry(
        frame: CGRect(x: 0, y: 0, width: 1728, height: 1117),
        visibleFrame: CGRect(x: 0, y: 0, width: 1728, height: 1093),
        hasNotch: true, notchHalfWidth: 110
    )
    let plain = ScreenGeometry(
        frame: CGRect(x: 0, y: 0, width: 1440, height: 900),
        visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 876),
        hasNotch: false, notchHalfWidth: 0
    )
    let external = ScreenGeometry(
        // Second display to the right of a notched internal.
        frame: CGRect(x: 1512, y: 0, width: 2560, height: 1440),
        visibleFrame: CGRect(x: 1512, y: 0, width: 2560, height: 1416),
        hasNotch: false, notchHalfWidth: 0
    )
    let narrow = ScreenGeometry(
        frame: CGRect(x: 0, y: 0, width: 360, height: 600),
        visibleFrame: CGRect(x: 0, y: 0, width: 360, height: 576),
        hasNotch: false, notchHalfWidth: 0
    )

    // Notched 14": compact orb sits right of the notch, in the menu-bar band.
    do {
        let f = IslandLayout.frame(
            screen: notch14, mode: .compact, placement: .topCenter,
            fullscreenApp: false, state: .idle
        )
        c.expect(f.hidden == false, "compact hidden")
        c.expect(f.orbDiameter == 40, "compact diameter")
        c.expect(abs(f.rect.minX - (756 + 100 + 10)) < 0.5, "notch-beside x: \(f.rect)")
        // Tucked 2 pt below the screen top, overlapping the menu-bar band.
        c.expect(abs(f.rect.maxY - (982 - 2)) < 0.5, "menu-band y: \(f.rect)")
        c.expect(notch14.frame.contains(f.rect), "compact must be on screen")
    }

    // Notched 16": same rule, wider notch.
    do {
        let f = IslandLayout.frame(
            screen: notch16, mode: .compact, placement: .topCenter,
            fullscreenApp: false, state: .listening
        )
        c.expect(abs(f.rect.minX - (864 + 110 + 10)) < 0.5, "16in x: \(f.rect)")
    }

    // Notched expanded: island centered under the notch, dropping down.
    do {
        let f = IslandLayout.frame(
            screen: notch14, mode: .expanded(contentHeight: 120), placement: .topCenter,
            fullscreenApp: false, state: .acting
        )
        c.expect(abs(f.rect.midX - 756) < 0.5, "island centered: \(f.rect)")
        c.expect(abs(f.rect.maxY - 958) < 0.5, "island top at visible top: \(f.rect)")
        c.expect(abs(f.rect.height - 152) < 0.5, "island height 120+32: \(f.rect)")
        c.expect(f.orbDiameter == 28, "expanded orb badge")
    }

    // Non-notched placements for the floating orb.
    do {
        let top = IslandLayout.frame(
            screen: plain, mode: .compact, placement: .topCenter,
            fullscreenApp: false, state: .idle
        )
        c.expect(abs(top.rect.midX - 720) < 0.5, "topCenter x")
        c.expect(abs(top.rect.maxY - (876 - 6)) < 0.5, "topCenter y")

        let near = IslandLayout.frame(
            screen: plain, mode: .compact, placement: .nearMenuBar,
            fullscreenApp: false, state: .idle
        )
        c.expect(abs(near.rect.maxX - (1440 - 8)) < 0.5, "nearMenuBar trailing")
        c.expect(abs(near.rect.maxY - (876 - 6)) < 0.5, "nearMenuBar y")

        let bottom = IslandLayout.frame(
            screen: plain, mode: .compact, placement: .bottomRight,
            fullscreenApp: false, state: .idle
        )
        c.expect(abs(bottom.rect.maxX - (1440 - 8)) < 0.5, "bottomRight trailing")
        c.expect(abs(bottom.rect.minY - 12) < 0.5, "bottomRight y")
    }

    // External display: coordinates follow the screen's own origin.
    do {
        let f = IslandLayout.frame(
            screen: external, mode: .compact, placement: .topCenter,
            fullscreenApp: false, state: .idle
        )
        c.expect(abs(f.rect.midX - (1512 + 1280)) < 0.5, "external center x: \(f.rect)")
        c.expect(external.frame.contains(f.rect), "external compact on screen")
        let island = IslandLayout.frame(
            screen: external, mode: .expanded(contentHeight: 200), placement: .topCenter,
            fullscreenApp: false, state: .needsConfirm
        )
        c.expect(abs(island.rect.midX - (1512 + 1280)) < 0.5, "external island center")
        c.expect(external.visibleFrame.contains(island.rect), "external island visible")
    }

    // Expanded island clamps height and width.
    do {
        let tall = IslandLayout.frame(
            screen: plain, mode: .expanded(contentHeight: 1000), placement: .topCenter,
            fullscreenApp: false, state: .acting
        )
        c.expect(tall.rect.height == 320, "island max height")

        let flat = IslandLayout.frame(
            screen: plain, mode: .expanded(contentHeight: 0), placement: .topCenter,
            fullscreenApp: false, state: .acting
        )
        c.expect(flat.rect.height == 64, "island min height")

        let wide = IslandLayout.frame(
            screen: narrow, mode: .expanded(contentHeight: 100), placement: .topCenter,
            fullscreenApp: false, state: .acting
        )
        c.expect(wide.rect.width == 360 - 32, "island clamps to narrow screen")
        c.expect(narrow.frame.contains(wide.rect), "narrow island on screen")
    }

    // Fullscreen apps: auto-hide unless confirmation is needed.
    for state in OrbState.allCases {
        let f = IslandLayout.frame(
            screen: notch14, mode: .compact, placement: .topCenter,
            fullscreenApp: true, state: state
        )
        if state == .needsConfirm {
            c.expect(f.hidden == false, "needsConfirm must show over fullscreen")
        } else {
            c.expect(f.hidden == true, "\(state) must hide over fullscreen")
        }
    }
}
