import EvieOrb
import Foundation

/// R1: headless Metal renderer checks (offscreen textures only, no windows).
func runRendererChecks(_ c: inout CheckContext) {
    guard let renderer = try? OrbRenderer() else {
        c.expect(false, "no Metal device — cannot verify renderer on this Mac")
        return
    }
    let idle = OrbParameters.target(for: .idle, level: 0, reduceMotion: false, appearance: .dark)

    // Renders at compact and expanded sizes.
    guard let small = try? renderer.render(
        sizePoints: CGSize(width: 40, height: 40), scale: 2,
        params: idle, time: 1.0, rotation: 0, appearance: .dark
    ) else {
        c.expect(false, "render failed at 40pt")
        return
    }
    c.expect(small.width == 80 && small.height == 80, "40pt@2x must be 80px, got \(small.width)x\(small.height)")
    guard let big = try? renderer.render(
        sizePoints: CGSize(width: 120, height: 120), scale: 2,
        params: idle, time: 1.0, rotation: 0, appearance: .dark
    ) else {
        c.expect(false, "render failed at 120pt")
        return
    }
    c.expect(big.width == 240 && big.height == 240, "120pt@2x must be 240px")

    // Ring band is lit, corners are transparent, core is darker than the band.
    let cx = big.width / 2, cy = big.height / 2
    let ringX = cx + Int(Float(big.width / 2) * idle.radius)
    let ringA = big.alphaAt(x: ringX, y: cy)
    c.expect(ringA > 0.1, "ring band too dim (alpha \(ringA))")
    c.expect(big.alphaAt(x: 2, y: 2) < 0.02, "corner not transparent")
    c.expect(big.alphaAt(x: big.width - 3, y: 3) < 0.02, "corner not transparent")
    let coreA = big.alphaAt(x: cx, y: cy)
    c.expect(coreA < ringA, "core (\(coreA)) not darker than band (\(ringA))")

    // Light appearance paints a faint dark halo behind the ring.
    let lightParams = OrbParameters.target(for: .idle, level: 0, reduceMotion: false, appearance: .light)
    if let light = try? renderer.render(
        sizePoints: CGSize(width: 120, height: 120), scale: 2,
        params: lightParams, time: 1.0, rotation: 0, appearance: .light
    ) {
        let lightCore = light.alphaAt(x: cx, y: cy)
        c.expect(lightCore > coreA + 0.03, "light halo missing (light \(lightCore) vs dark \(coreA))")
        let px = light.maxRGBAt(x: cx, y: cy)
        c.expect(px < 0.25, "halo not dark (\(px))")
    } else {
        c.expect(false, "render failed for light appearance")
    }

    // Deterministic: same inputs, identical bytes.
    if let a = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: idle, time: 2.5, rotation: 0.7, appearance: .dark
    ), let b = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: idle, time: 2.5, rotation: 0.7, appearance: .dark
    ) {
        c.expect(a.bgra == b.bgra, "same inputs gave different bytes")
    } else {
        c.expect(false, "determinism renders failed")
    }

    // Animated: time advance moves pixels for a flowing state.
    let listen = OrbParameters.target(for: .listening, level: 0.7, reduceMotion: false, appearance: .dark)
    if let t0 = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: listen, time: 0.0, rotation: 0, appearance: .dark
    ), let t1 = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: listen, time: 1.0, rotation: 0.3, appearance: .dark
    ) {
        c.expect(t0.meanAbsDiff(t1) > 1.0 / 255.0, "listening orb does not animate")
    } else {
        c.expect(false, "animation renders failed")
    }

    // Energy responds: louder = brighter.
    let dim = OrbParameters.target(for: .listening, level: 0.05, reduceMotion: false, appearance: .dark)
    let loud = OrbParameters.target(for: .listening, level: 1.0, reduceMotion: false, appearance: .dark)
    if let fDim = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: dim, time: 0.5, rotation: 0, appearance: .dark
    ), let fLoud = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: loud, time: 0.5, rotation: 0, appearance: .dark
    ) {
        c.expect(fLoud.meanAlpha > fDim.meanAlpha + 0.01, "energy does not brighten")
    } else {
        c.expect(false, "energy renders failed")
    }

    // Offline is thinner/dimmer than idle.
    let off = OrbParameters.target(for: .offline, level: 0, reduceMotion: false, appearance: .dark)
    if let fOff = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: off, time: 0.5, rotation: 0, appearance: .dark
    ), let fIdle = try? renderer.render(
        sizePoints: CGSize(width: 80, height: 80), scale: 2,
        params: idle, time: 0.5, rotation: 0, appearance: .dark
    ) {
        c.expect(fOff.meanAlpha < fIdle.meanAlpha, "offline not dimmer than idle")
    } else {
        c.expect(false, "offline renders failed")
    }

    // Single-frame CPU cost is sane even in a debug build.
    c.expect(
        renderer.lastCPUTimeMilliseconds < 100,
        "single frame too slow: \(renderer.lastCPUTimeMilliseconds) ms CPU"
    )

    // Small frame is not blank either.
    c.expect(small.meanAlpha > 0.005, "40pt frame is blank")
}
