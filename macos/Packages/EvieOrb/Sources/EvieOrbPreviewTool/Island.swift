import AppKit
import EvieOrb
import Foundation

// R7 island expand/collapse sequence. Geometry comes from the REAL
// IslandLayout (simulated 14" notched screen), orb pixels are REAL renders,
// content is rasterized from the REAL placeholder views. Only the wallpaper,
// menu bar, and notch are drawn mocks. No windows.

private let canvasW: CGFloat = 756
private let canvasH: CGFloat = 300
// Canvas shows AppKit x 378..1134, y 682..982 of the simulated screen.
private let canvasOriginX: CGFloat = 378
private let canvasOriginY: CGFloat = 682

private func canvasRect(_ r: CGRect) -> NSRect {
    NSRect(
        x: r.minX - canvasOriginX, y: r.minY - canvasOriginY,
        width: r.width, height: r.height
    )
}

private func smooth(_ t: Double) -> Double {
    let x = min(1, max(0, t))
    return x * x * (3 - 2 * x)
}

private func lerpRect(_ a: CGRect, _ b: CGRect, _ t: Double) -> CGRect {
    CGRect(
        x: a.minX + (b.minX - a.minX) * t,
        y: a.minY + (b.minY - a.minY) * t,
        width: a.width + (b.width - a.width) * t,
        height: a.height + (b.height - a.height) * t
    )
}

func runIsland(opts: [String: String]) {
    let bgName = opts["bg"] ?? "dark"
    let dark = bgName != "light"
    let appearance: OrbAppearance = dark ? .dark : .light
    let fps = 30.0
    let outdir = opts["outdir"] ?? "orb-island"
    let prefix = opts["prefix"] ?? "f"
    try? FileManager.default.createDirectory(atPath: outdir, withIntermediateDirectories: true)
    guard let renderer = try? OrbRenderer() else { exit(1) }

    // Simulated 14" notched screen + real layout frames.
    let screen = ScreenGeometry(
        frame: CGRect(x: 0, y: 0, width: 1512, height: 982),
        visibleFrame: CGRect(x: 0, y: 0, width: 1512, height: 958),
        hasNotch: true, notchHalfWidth: 95
    )
    let compact = IslandLayout.frame(
        screen: screen, mode: .compact, placement: .topCenter,
        fullscreenApp: false, state: .idle
    ).rect
    let planContent = PlaceholderPlanSteps()
    let confirmContent = PlaceholderConfirmCard()
    let planRect = IslandLayout.frame(
        screen: screen, mode: .expanded(contentHeight: planContent.contentHeight),
        placement: .topCenter, fullscreenApp: false, state: .acting
    ).rect
    let confirmRect = IslandLayout.frame(
        screen: screen, mode: .expanded(contentHeight: confirmContent.contentHeight),
        placement: .topCenter, fullscreenApp: false, state: .needsConfirm
    ).rect

    // Content slot inside the island (mirrors IslandHost.layoutContainer).
    func slotRect(island: CGRect) -> CGRect {
        CGRect(
            x: island.minX + 12 + 28 + 10, y: island.minY + 10,
            width: island.width - (12 + 28 + 10) - 12,
            height: island.height - 20
        )
    }
    func badgeRect(island: CGRect, diameter: CGFloat) -> CGRect {
        CGRect(
            x: island.minX + 12, y: island.maxY - 12 - diameter,
            width: diameter, height: diameter
        )
    }

    // Rasterize content views once at slot width.
    planContent.view.frame = NSRect(
        x: 0, y: 0, width: slotRect(island: planRect).width,
        height: planContent.contentHeight
    )
    confirmContent.view.frame = NSRect(
        x: 0, y: 0, width: slotRect(island: confirmRect).width,
        height: confirmContent.contentHeight
    )
    guard let planImg = rasterize(planContent.view),
          let confirmImg = rasterize(confirmContent.view)
    else {
        fputs("island: content raster failed\n", stderr)
        exit(1)
    }

    // Island background rasters (real NSVisualEffectView, flat headless).
    func backgroundImage(size: CGSize) -> NSImage? {
        let fx = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        fx.material = dark ? .hudWindow : .popover
        fx.state = .active
        fx.wantsLayer = true
        fx.layer?.cornerRadius = 20
        fx.layer?.masksToBounds = true
        fx.layer?.borderWidth = 1
        fx.layer?.borderColor = NSColor.white.withAlphaComponent(dark ? 0.12 : 0.4).cgColor
        return rasterize(fx)
    }
    guard let planBg = backgroundImage(size: planRect.size),
          let confirmBg = backgroundImage(size: confirmRect.size)
    else {
        fputs("island: background raster failed\n", stderr)
        exit(1)
    }

    // Choreography: (phase label, state, seconds, island rect or nil, content img or nil, bg or nil).
    struct Phase {
        let label: String
        let state: OrbState
        let seconds: Double
        let from: CGRect?
        let to: CGRect?
        let content: NSImage?
        let contentSlot: CGRect?
        let bg: NSImage?
        let bgRect: CGRect?
    }
    let phases: [Phase] = [
        Phase(label: "compact", state: .idle, seconds: 1.0,
              from: nil, to: nil, content: nil, contentSlot: nil, bg: nil, bgRect: nil),
        Phase(label: "expand", state: .acting, seconds: 0.8,
              from: compact, to: planRect, content: planImg,
              contentSlot: slotRect(island: planRect), bg: planBg, bgRect: planRect),
        Phase(label: "plan steps", state: .acting, seconds: 2.0,
              from: planRect, to: planRect, content: planImg,
              contentSlot: slotRect(island: planRect), bg: planBg, bgRect: planRect),
        Phase(label: "confirm", state: .needsConfirm, seconds: 0.5,
              from: planRect, to: confirmRect, content: confirmImg,
              contentSlot: slotRect(island: confirmRect), bg: confirmBg, bgRect: confirmRect),
        Phase(label: "confirm card", state: .needsConfirm, seconds: 2.0,
              from: confirmRect, to: confirmRect, content: confirmImg,
              contentSlot: slotRect(island: confirmRect), bg: confirmBg, bgRect: confirmRect),
        Phase(label: "collapse", state: .idle, seconds: 0.8,
              from: confirmRect, to: compact, content: nil,
              contentSlot: nil, bg: confirmBg, bgRect: confirmRect),
        Phase(label: "compact", state: .idle, seconds: 1.0,
              from: nil, to: nil, content: nil, contentSlot: nil, bg: nil, bgRect: nil),
    ]

    let machine = OrbStateMachine(appearance: appearance)
    let quiet = SyntheticLevelSource(seed: 9, profile: .quiet)
    var t = 0.0
    var index = 0
    // Pre-roll.
    for _ in 0..<60 {
        quiet.advance(by: 1.0 / fps)
        _ = machine.update(dt: 1.0 / fps, inputLevel: 0, outputLevel: 0)
    }

    // Wallpaper gradient stops.
    let topColor = dark
        ? NSColor(red: 0.13, green: 0.14, blue: 0.18, alpha: 1)
        : NSColor(red: 0.87, green: 0.89, blue: 0.92, alpha: 1)
    let bottomColor = dark
        ? NSColor(red: 0.05, green: 0.05, blue: 0.07, alpha: 1)
        : NSColor(red: 0.96, green: 0.96, blue: 0.98, alpha: 1)
    let menuColor = dark
        ? NSColor(white: 0.0, alpha: 0.35)
        : NSColor(white: 1.0, alpha: 0.55)

    for phase in phases {
        machine.setState(phase.state)
        let n = Int(phase.seconds * fps)
        for fi in 0..<n {
            let dt = 1.0 / fps
            quiet.advance(by: dt)
            let params = machine.update(dt: dt, inputLevel: 0, outputLevel: 0)
            t += dt
            let k = smooth(Double(fi) / Double(max(n - 1, 1)))

            // Interpolated island rect (or compact when no island phase).
            let islandNow: CGRect? = {
                if let from = phase.from, let to = phase.to {
                    return lerpRect(from, to, k)
                }
                return nil
            }()
            // Morph progress for orb slot: 0 = compact orb, 1 = badge.
            let morphK: Double = {
                switch phase.label {
                case "expand": return k
                case "plan steps", "confirm", "confirm card": return 1
                case "collapse": return 1 - k
                default: return 0
                }
            }()
            let badgeIsland = (phase.label == "collapse") ? (phase.from ?? planRect) : (phase.to ?? planRect)
            let badgeNow = badgeRect(island: badgeIsland, diameter: 28)
            let orbSlot = lerpRect(compact, badgeNow, morphK)
            let orbDiameter = 40 + (28 - 40) * morphK

            guard let frame = try? renderer.render(
                sizePoints: CGSize(width: 40, height: 40), scale: 2,
                params: params, time: t, rotation: machine.rotationAngle,
                appearance: appearance, orbitAngle: Float(t * 2)
            ), let orb = orbNSImage(frame) else { continue }

            let canvas = NSImage(size: NSSize(width: canvasW, height: canvasH))
            canvas.lockFocus()
            // Wallpaper.
            if let grad = NSGradient(starting: bottomColor, ending: topColor) {
                grad.draw(in: NSRect(x: 0, y: 0, width: canvasW, height: canvasH), angle: 90)
            }
            // Menu bar + notch.
            menuColor.setFill()
            NSRect(x: 0, y: canvasH - 24, width: canvasW, height: 24).fill()
            NSColor.black.setFill()
            let notch = NSRect(x: canvasW / 2 - 95, y: canvasH - 24, width: 190, height: 24)
            NSBezierPath(roundedRect: notch, xRadius: 0, yRadius: 9).fill()
            // Fake menu extras (left: app name, right: clock) for scale.
            let menuAttrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium),
                .foregroundColor: dark ? NSColor.white : NSColor.black,
            ]
            "Evie".draw(at: NSPoint(x: 12, y: canvasH - 19), withAttributes: menuAttrs)
            "09:41".draw(at: NSPoint(x: canvasW - 52, y: canvasH - 19), withAttributes: menuAttrs)

            // Island background + content (fade in/out at the ends).
            let contentAlpha: CGFloat = {
                switch phase.label {
                case "expand": return CGFloat(smooth((Double(fi) / Double(max(n - 1, 1)) - 0.35) / 0.65))
                case "collapse": return CGFloat(1 - smooth(Double(fi) / Double(max(n - 1, 1)) * 1.6))
                case "confirm": return 1
                case "plan steps", "confirm card": return 1
                default: return 0
                }
            }()
            if let now = islandNow, contentAlpha > 0.01, let bgImg = phase.bg {
                bgImg.draw(
                    in: canvasRect(now), from: .zero,
                    operation: .sourceOver, fraction: contentAlpha
                )
            }
            if contentAlpha > 0.01, let img = phase.content, let slot = phase.contentSlot {
                // During morphs the slot rides the interpolated island.
                let slotNow: CGRect = {
                    guard let now = islandNow, let bgR = phase.bgRect else { return slot }
                    let dx = now.minX - bgR.minX
                    let dy = now.minY - bgR.minY
                    return slot.offsetBy(dx: dx, dy: dy)
                }()
                img.draw(
                    in: canvasRect(slotNow), from: .zero,
                    operation: .sourceOver, fraction: contentAlpha
                )
            }
            // Orb on top.
            let orbRect = canvasRect(orbSlot)
            orb.draw(
                in: NSRect(
                    x: orbRect.minX, y: orbRect.minY,
                    width: orbDiameter, height: orbDiameter
                ),
                from: .zero, operation: .sourceOver, fraction: 1
            )
            canvas.unlockFocus()
            burnLabel("island · \(phase.label)", into: canvas, at: NSPoint(x: 8, y: 8), dark: dark)
            let path = String(format: "\(outdir)/\(prefix)%04d.png", index)
            _ = saveCanvas(canvas, url: URL(fileURLWithPath: path))
            index += 1
        }
    }
    print("wrote \(index) island frames to \(outdir)/")
}
