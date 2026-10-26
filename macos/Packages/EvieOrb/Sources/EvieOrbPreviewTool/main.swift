import CoreGraphics
import EvieOrb
import Foundation
import ImageIO

// Headless orb preview tool: renders offscreen frames to PNG (stills and
// sequences for GIF/MP4 assembly via ffmpeg). Never opens a window.

func writePNG(frame: OrbFrame, background: (r: Float, g: Float, b: Float)?, url: URL) -> Bool {
    var bytes = frame.bgra
    if let bg = background {
        // Premultiplied src-over composite onto a flat background.
        for i in stride(from: 0, to: bytes.count, by: 4) {
            let a = Float(bytes[i + 3]) / 255
            let inv = 1 - a
            bytes[i] = UInt8(min(255, Float(bytes[i]) + bg.b * 255 * inv))
            bytes[i + 1] = UInt8(min(255, Float(bytes[i + 1]) + bg.g * 255 * inv))
            bytes[i + 2] = UInt8(min(255, Float(bytes[i + 2]) + bg.r * 255 * inv))
            bytes[i + 3] = 255
        }
    }
    let data = Data(bytes)
    guard let provider = CGDataProvider(data: data as CFData) else { return false }
    let bitmapInfo = CGBitmapInfo(
        rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue
            | CGBitmapInfo.byteOrder32Little.rawValue
    )
    guard let image = CGImage(
        width: frame.width, height: frame.height,
        bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: frame.width * 4,
        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: bitmapInfo,
        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
    ) else { return false }
    guard let dest = CGImageDestinationCreateWithURL(
        url as CFURL, "public.png" as CFString, 1, nil
    ) else { return false }
    CGImageDestinationAddImage(dest, image, nil)
    return CGImageDestinationFinalize(dest)
}

func background(named: String) -> (r: Float, g: Float, b: Float)? {
    switch named {
    case "dark": return (0.07, 0.07, 0.09)
    case "light": return (0.93, 0.93, 0.95)
    case "none": return nil
    default: return (0.07, 0.07, 0.09)
    }
}

func parseState(_ s: String) -> OrbState? {
    switch s {
    case "idle": return .idle
    case "listening": return .listening
    case "thinking": return .thinking
    case "acting": return .acting
    case "speaking": return .speaking
    case "needs_confirm": return .needsConfirm
    case "success": return .success
    case "error": return .error
    case "offline": return .offline
    default: return nil
    }
}

func args() -> [String: String] {
    var out: [String: String] = [:]
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        if a.hasPrefix("--"), let v = it.next() {
            out[String(a.dropFirst(2))] = v
        } else if !a.hasPrefix("--"), out["cmd"] == nil {
            out["cmd"] = a
        }
    }
    return out
}

let opts = args()
let cmd = opts["cmd"] ?? "help"

func fail(_ msg: String) -> Never {
    fputs("evie-orb-preview: \(msg)\n", stderr)
    exit(1)
}

guard cmd == "still" || cmd == "states" || cmd == "sequence" else {
    print("""
    usage:
      evie-orb-preview still --state <name> --size <pt> [--scale 2] [--time 1.0]
        [--bg dark|light|none] [--level 0.5] [--out frame.png]
      evie-orb-preview states --size <pt> [--scale 2] [--bg dark|light]
        [--outdir dir/]
      evie-orb-preview sequence --state <name> --seconds <s> --fps <n>
        --size <pt> [--scale 2] [--bg dark|light] [--profile speech|playback|quiet]
        [--outdir dir/] [--prefix f]
    """)
    exit(cmd == "help" ? 0 : 1)
}

let renderer: OrbRenderer
do {
    renderer = try OrbRenderer()
} catch {
    fail("renderer init: \(error)")
}

let size = Double(opts["size"] ?? "120") ?? 120
let scale = Double(opts["scale"] ?? "2") ?? 2
let bgName = opts["bg"] ?? "dark"
let bg = background(named: bgName)
let appearance: OrbAppearance = (bgName == "light") ? .light : .dark

if cmd == "still" {
    guard let state = parseState(opts["state"] ?? "") else { fail("bad --state") }
    let time = Double(opts["time"] ?? "1.0") ?? 1.0
    let level = Float(opts["level"] ?? "0.5") ?? 0.5
    let machine = OrbStateMachine(appearance: appearance)
    machine.setState(state)
    // Settle springs, then capture.
    for _ in 0..<90 {
        _ = machine.update(dt: 1.0 / 60.0, inputLevel: level, outputLevel: level)
    }
    let params = machine.current
    let frame: OrbFrame
    do {
        frame = try renderer.render(
            sizePoints: CGSize(width: size, height: size), scale: scale,
            params: params, time: time, rotation: machine.rotationAngle,
            appearance: appearance, orbitAngle: Float(time * 2)
        )
    } catch {
        fail("render: \(error)")
    }
    let out = opts["out"] ?? "frame.png"
    if !writePNG(frame: frame, background: bg, url: URL(fileURLWithPath: out)) {
        fail("could not write \(out)")
    }
    print("wrote \(out) (\(frame.width)x\(frame.height))")
} else if cmd == "states" {
    let outdir = opts["outdir"] ?? "orb-states"
    try? FileManager.default.createDirectory(atPath: outdir, withIntermediateDirectories: true)
    for state in OrbState.allCases {
        let machine = OrbStateMachine(appearance: appearance)
        machine.setState(state)
        for _ in 0..<90 {
            _ = machine.update(dt: 1.0 / 60.0, inputLevel: 0.6, outputLevel: 0.6)
        }
        let frame = try! renderer.render(
            sizePoints: CGSize(width: size, height: size), scale: scale,
            params: machine.current, time: 1.0, rotation: machine.rotationAngle,
            appearance: appearance, orbitAngle: 2.0
        )
        let path = "\(outdir)/\(state.rawValue).png"
        _ = writePNG(frame: frame, background: bg, url: URL(fileURLWithPath: path))
        print("wrote \(path)")
    }
} else {
    // sequence: drive the machine with a synthetic source for realistic motion.
    guard let state = parseState(opts["state"] ?? "") else { fail("bad --state") }
    let seconds = Double(opts["seconds"] ?? "3") ?? 3
    let fps = Double(opts["fps"] ?? "30") ?? 30
    let profile: SyntheticLevelSource.Profile
    switch opts["profile"] ?? "speech" {
    case "playback": profile = .playback
    case "quiet": profile = .quiet
    default: profile = .speech
    }
    let outdir = opts["outdir"] ?? "orb-seq"
    let prefix = opts["prefix"] ?? "f"
    try? FileManager.default.createDirectory(atPath: outdir, withIntermediateDirectories: true)
    let machine = OrbStateMachine(appearance: appearance)
    let source = SyntheticLevelSource(seed: 3, profile: profile)
    machine.setState(state)
    let n = Int(seconds * fps)
    // Pre-roll so springs and envelopes are alive at frame 0.
    for _ in 0..<60 {
        source.advance(by: 1.0 / fps)
        _ = machine.update(dt: 1.0 / fps, inputLevel: source.inputLevel, outputLevel: source.outputLevel)
    }
    var t = 0.0
    for i in 0..<n {
        source.advance(by: 1.0 / fps)
        let params = machine.update(dt: 1.0 / fps, inputLevel: source.inputLevel, outputLevel: source.outputLevel)
        let frame = try! renderer.render(
            sizePoints: CGSize(width: size, height: size), scale: scale,
            params: params, time: t, rotation: machine.rotationAngle,
            appearance: appearance, orbitAngle: Float(t * 2)
        )
        let path = String(format: "\(outdir)/\(prefix)%04d.png", i)
        _ = writePNG(frame: frame, background: bg, url: URL(fileURLWithPath: path))
        t += 1.0 / fps
    }
    print("wrote \(n) frames to \(outdir)/")
}
