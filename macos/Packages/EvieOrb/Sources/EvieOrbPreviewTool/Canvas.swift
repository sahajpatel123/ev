import AppKit
import EvieOrb
import Foundation

// Canvas compositing for R7 previews: NSImage canvases (headless lockFocus),
// real orb pixels + real layout + real placeholder views. No windows.

func orbNSImage(_ frame: OrbFrame) -> NSImage? {
    let data = Data(frame.bgra)
    guard let provider = CGDataProvider(data: data as CFData) else { return nil }
    let bitmapInfo = CGBitmapInfo(
        rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue
            | CGBitmapInfo.byteOrder32Little.rawValue
    )
    guard let cg = CGImage(
        width: frame.width, height: frame.height,
        bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: frame.width * 4,
        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: bitmapInfo,
        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
    ) else { return nil }
    let img = NSImage(size: NSSize(width: frame.width, height: frame.height))
    img.addRepresentation(NSBitmapImageRep(cgImage: cg))
    return img
}

func saveCanvas(_ image: NSImage, url: URL) -> Bool {
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:])
    else { return false }
    do {
        try png.write(to: url)
        return true
    } catch {
        return false
    }
}

func rasterize(_ view: NSView) -> NSImage? {
    view.layoutSubtreeIfNeeded()
    guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return nil }
    view.cacheDisplay(in: view.bounds, to: rep)
    guard let cg = rep.cgImage else { return nil }
    let img = NSImage(size: rep.size)
    img.addRepresentation(NSBitmapImageRep(cgImage: cg))
    return img
}

func burnLabel(
    _ text: String, into image: NSImage, at point: NSPoint,
    size: CGFloat = 13, dark: Bool = true
) {
    image.lockFocus()
    let color = dark ? NSColor.white.withAlphaComponent(0.85) : NSColor.black.withAlphaComponent(0.7)
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.monospacedDigitSystemFont(ofSize: size, weight: .medium),
        .foregroundColor: color,
    ]
    (" " + text + " ").draw(at: point, withAttributes: attrs)
    image.unlockFocus()
}

struct DriveState {
    var machine: OrbStateMachine
    var source: SyntheticLevelSource
    var time: TimeInterval = 0

    mutating func frame(
        renderer: OrbRenderer, diameter: CGFloat, scale: CGFloat,
        fps: Double, state: OrbState, appearance: OrbAppearance
    ) -> OrbFrame? {
        let dt = 1.0 / fps
        if machine.state != state { machine.setState(state) }
        source.advance(by: dt)
        let params = machine.update(
            dt: dt, inputLevel: source.inputLevel, outputLevel: source.outputLevel
        )
        time += dt
        return try? renderer.render(
            sizePoints: CGSize(width: diameter, height: diameter), scale: scale,
            params: params, time: time, rotation: machine.rotationAngle,
            appearance: appearance, orbitAngle: Float(time * 2)
        )
    }
}

func profileForTour(state: OrbState) -> SyntheticLevelSource.Profile {
    switch state {
    case .listening: return .speech
    case .speaking: return .playback
    default: return .quiet
    }
}

// MARK: - Tour: every state and transition in one clip

func runTour(opts: [String: String]) {
    let bgName = opts["bg"] ?? "dark"
    let bg: NSColor = (bgName == "light")
        ? NSColor(red: 0.93, green: 0.93, blue: 0.95, alpha: 1)
        : NSColor(red: 0.07, green: 0.07, blue: 0.09, alpha: 1)
    let appearance: OrbAppearance = (bgName == "light") ? .light : .dark
    let fps = 30.0
    let outdir = opts["outdir"] ?? "orb-tour"
    let prefix = opts["prefix"] ?? "f"
    try? FileManager.default.createDirectory(atPath: outdir, withIntermediateDirectories: true)
    guard let renderer = try? OrbRenderer() else { exit(1) }

    // (state, seconds): idle -> listening -> thinking -> acting -> speaking ->
    // needs_confirm -> success -> idle -> error -> idle -> offline -> idle.
    let script: [(OrbState, Double)] = [
        (.idle, 1.5), (.listening, 3.0), (.thinking, 2.5), (.acting, 2.5),
        (.speaking, 3.0), (.needsConfirm, 2.5), (.success, 1.8), (.idle, 1.2),
        (.error, 1.5), (.idle, 1.0), (.offline, 2.0), (.idle, 1.5),
    ]
    var index = 0
    // One continuous machine so transitions are the real spring motion.
    let machine = OrbStateMachine(appearance: appearance)
    let speech = SyntheticLevelSource(seed: 21, profile: .speech)
    let playback = SyntheticLevelSource(seed: 22, profile: .playback)
    var t = 0.0
    for (state, seconds) in script {
        machine.setState(state)
        let n = Int(seconds * fps)
        for _ in 0..<n {
            let dt = 1.0 / fps
            speech.advance(by: dt)
            playback.advance(by: dt)
            let params = machine.update(
                dt: dt, inputLevel: speech.inputLevel, outputLevel: playback.outputLevel
            )
            t += dt
            guard let frame = try? renderer.render(
                sizePoints: CGSize(width: 120, height: 120), scale: 2,
                params: params, time: t, rotation: machine.rotationAngle,
                appearance: appearance, orbitAngle: Float(t * 2)
            ), let orb = orbNSImage(frame) else { continue }
            let canvas = NSImage(size: NSSize(width: 240, height: 240))
            canvas.lockFocus()
            bg.setFill()
            NSRect(x: 0, y: 0, width: 240, height: 240).fill()
            orb.draw(in: NSRect(x: 0, y: 0, width: 240, height: 240))
            canvas.unlockFocus()
            burnLabel(state.rawValue, into: canvas, at: NSPoint(x: 8, y: 8), dark: bgName != "light")
            let path = String(format: "\(outdir)/\(prefix)%04d.png", index)
            _ = saveCanvas(canvas, url: URL(fileURLWithPath: path))
            index += 1
        }
    }
    print("wrote \(index) tour frames to \(outdir)/")
}

// MARK: - Side-by-side listening (same envelope, dark + light)

func runSideBySide(opts: [String: String]) {
    let fps = 30.0
    let seconds = Double(opts["seconds"] ?? "4") ?? 4
    let outdir = opts["outdir"] ?? "orb-sbs"
    let prefix = opts["prefix"] ?? "f"
    try? FileManager.default.createDirectory(atPath: outdir, withIntermediateDirectories: true)
    guard let renderer = try? OrbRenderer() else { exit(1) }

    let darkMachine = OrbStateMachine(appearance: .dark)
    let lightMachine = OrbStateMachine(appearance: .light)
    darkMachine.setState(.listening)
    lightMachine.setState(.listening)
    let source = SyntheticLevelSource(seed: 5, profile: .speech)
    // Pre-roll.
    for _ in 0..<60 {
        source.advance(by: 1.0 / fps)
        _ = darkMachine.update(dt: 1.0 / fps, inputLevel: source.inputLevel, outputLevel: 0)
        _ = lightMachine.update(dt: 1.0 / fps, inputLevel: source.inputLevel, outputLevel: 0)
    }
    let darkBg = NSColor(red: 0.07, green: 0.07, blue: 0.09, alpha: 1)
    let lightBg = NSColor(red: 0.93, green: 0.93, blue: 0.95, alpha: 1)
    var t = 0.0
    let n = Int(seconds * fps)
    for i in 0..<n {
        let dt = 1.0 / fps
        source.advance(by: dt)
        let pDark = darkMachine.update(dt: dt, inputLevel: source.inputLevel, outputLevel: 0)
        let pLight = lightMachine.update(dt: dt, inputLevel: source.inputLevel, outputLevel: 0)
        t += dt
        guard let fDark = try? renderer.render(
            sizePoints: CGSize(width: 120, height: 120), scale: 2,
            params: pDark, time: t, rotation: darkMachine.rotationAngle,
            appearance: .dark, orbitAngle: Float(t * 2)
        ), let fLight = try? renderer.render(
            sizePoints: CGSize(width: 120, height: 120), scale: 2,
            params: pLight, time: t, rotation: lightMachine.rotationAngle,
            appearance: .light, orbitAngle: Float(t * 2)
        ), let oDark = orbNSImage(fDark), let oLight = orbNSImage(fLight) else { continue }
        let canvas = NSImage(size: NSSize(width: 480, height: 240))
        canvas.lockFocus()
        darkBg.setFill()
        NSRect(x: 0, y: 0, width: 240, height: 240).fill()
        lightBg.setFill()
        NSRect(x: 240, y: 0, width: 240, height: 240).fill()
        oDark.draw(in: NSRect(x: 0, y: 0, width: 240, height: 240))
        oLight.draw(in: NSRect(x: 240, y: 0, width: 240, height: 240))
        canvas.unlockFocus()
        burnLabel("listening · dark", into: canvas, at: NSPoint(x: 8, y: 8))
        let path = String(format: "\(outdir)/\(prefix)%04d.png", i)
        _ = saveCanvas(canvas, url: URL(fileURLWithPath: path))
    }
    print("wrote \(n) side-by-side frames to \(outdir)/")
}
