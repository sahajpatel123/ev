import Darwin
import EvieOrb
import Foundation
import Metal

// R5 headless benchmark: policy-gated render loops per (state, fps, size),
// measuring process CPU %, per-frame encode CPU, and GPU time. No windows.

struct BenchResult: Codable {
    let state: String
    let runKind: String // "gated" (policy-gated render loop) or "link-stopped" (deployed idle)
    let targetFPS: Int
    let sizePt: Double
    let scale: Double
    let seconds: Double
    let framesRendered: Int
    let achievedFPS: Double
    let processCPUPct: Double
    let frameCPUMeanMs: Double
    let frameCPUP95Ms: Double
    let frameGPUMeanMs: Double
    let frameGPUMaxMs: Double // raw max over ALL frames (warmup included)
    let frameGPUSteadyMaxMs: Double // max excluding the first 10 warmup frames
    let gpuSamples: Int
}

func processCPUTime() -> Double {
    var u = rusage()
    getrusage(RUSAGE_SELF, &u)
    let user = Double(u.ru_utime.tv_sec) + Double(u.ru_utime.tv_usec) / 1_000_000
    let sys = Double(u.ru_stime.tv_sec) + Double(u.ru_stime.tv_usec) / 1_000_000
    return user + sys
}

func percentile(_ sorted: [Double], _ p: Double) -> Double {
    guard !sorted.isEmpty else { return 0 }
    let idx = min(sorted.count - 1, Int((p * Double(sorted.count)).rounded()))
    return sorted[idx]
}

func runBench(opts: [String: String]) {
    let seconds = Double(opts["seconds"] ?? "60") ?? 60
    let outdir = opts["outdir"] ?? "bench"
    let scale = Double(opts["scale"] ?? "2") ?? 2
    let stateFilter = (opts["states"] ?? "").split(separator: ",").map(String.init)
    let fpsList = (opts["fps"] ?? "60,30").split(separator: ",").compactMap { Int($0) }
    let sizeList = (opts["sizes"] ?? "40,120").split(separator: ",").compactMap { Double($0) }

    let renderer: OrbRenderer
    do {
        renderer = try OrbRenderer()
    } catch {
        fputs("bench: renderer init: \(error)\n", stderr)
        exit(1)
    }

    try? FileManager.default.createDirectory(atPath: outdir, withIntermediateDirectories: true)
    var results: [BenchResult] = []

    var states = OrbState.allCases
    if !stateFilter.isEmpty {
        states = states.filter { stateFilter.contains($0.rawValue) }
    }

    for state in states {
        for fps in fpsList {
            for size in sizeList {
                let r = benchOne(
                    renderer: renderer, state: state, fps: fps,
                    size: size, scale: scale, seconds: seconds
                )
                results.append(r)
                let name = (r.state as NSString).padding(toLength: 12, withPad: " ", startingAt: 0)
                print(String(
                    format: "%@ %2d fps %3.0fpt %5.1fs frames=%5d achieved=%5.1f cpu=%5.2f%% frameCPU=%.3f/%.3fms gpu=%.3f/%.3f/%.3fms(n=%d)",
                    name, r.targetFPS, r.sizePt, r.seconds, r.framesRendered,
                    r.achievedFPS, r.processCPUPct, r.frameCPUMeanMs, r.frameCPUP95Ms,
                    r.frameGPUMeanMs, r.frameGPUMaxMs, r.frameGPUSteadyMaxMs, r.gpuSamples
                ))
                fflush(stdout)
            }
        }
    }

    // Deployed-idle proof for static states (link stopped, 1 Hz re-check).
    for state in states where FramePacingPolicy.isStaticState(state) {
        let r = benchLinkStopped(state: state, seconds: seconds)
        results.append(r)
        let name = (r.state as NSString).padding(toLength: 12, withPad: " ", startingAt: 0)
        print(String(
            format: "%@ link-stopped %5.1fs violations=%d cpu=%5.3f%%",
            name, r.seconds, r.framesRendered, r.processCPUPct
        ))
        fflush(stdout)
    }

    let url = URL(fileURLWithPath: "\(outdir)/results.json")
    if let data = try? JSONEncoder().encode(results) {
        try? data.write(to: url)
        print("wrote \(url.path)")
    }
}

func benchOne(
    renderer: OrbRenderer, state: OrbState, fps: Int,
    size: Double, scale: Double, seconds: Double
) -> BenchResult {
    let machine = OrbStateMachine()
    let profile: SyntheticLevelSource.Profile =
        (state == .speaking) ? .playback : (state == .listening) ? .speech : .quiet
    let source = SyntheticLevelSource(seed: 11, profile: profile)
    machine.setState(state)

    // Pre-roll (not counted), except momentary states which are timed from t=0.
    let momentary = (state == .success || state == .error)
    if !momentary {
        for _ in 0..<90 {
            source.advance(by: 1.0 / Double(fps))
            _ = machine.update(
                dt: 1.0 / Double(fps),
                inputLevel: source.inputLevel, outputLevel: source.outputLevel
            )
        }
    }

    var cpuSamples: [Double] = []
    var gpuSamples: [Double] = []
    var gpuSteady: [Double] = []
    var frames = 0
    var lastRendered = machine.current
    var cachedTexture: MTLTexture? = nil
    var t = 0.0
    let dt = 1.0 / Double(fps)
    let ticks = Int(seconds / dt)

    let wallStart = Date()
    let cpuStart = processCPUTime()
    var nextDeadline = Date()
    for _ in 0..<ticks {
        nextDeadline = nextDeadline.addingTimeInterval(dt)
        source.advance(by: dt)
        let params = machine.update(
            dt: dt, inputLevel: source.inputLevel, outputLevel: source.outputLevel
        )
        let liveState = machine.state
        let changed = OrbParameters.diffMagnitude(params, lastRendered).max() ?? 0 > 0.0005
        let levelActive = max(source.inputLevel, source.outputLevel) > 0.01
        if FramePacingPolicy.shouldRender(
            state: liveState, visible: true, paramsChanged: changed, levelActive: levelActive
        ) {
            do {
                // One recycled texture per run, like the live drawable cycle.
                cachedTexture = try renderer.renderTexture(
                    sizePoints: CGSize(width: size, height: size), scale: scale,
                    params: params, time: t, rotation: machine.rotationAngle,
                    appearance: .dark, orbitAngle: Float(t * 2),
                    waitForCompletion: false, reusing: cachedTexture
                )
                frames += 1
                lastRendered = params
                if frames > 10 {
                    // First 10 frames carry one-time pipeline/GPU warmup.
                    cpuSamples.append(renderer.lastCPUTimeMilliseconds)
                }
                let g = renderer.lastGPUTimeMilliseconds
                if g >= 0 {
                    gpuSamples.append(g)
                    if frames > 10 { gpuSteady.append(g) }
                }
            } catch {
                // A failed frame is a failed benchmark, not a skipped sample.
                fputs("bench: render failed: \(error)\n", stderr)
                exit(1)
            }
        }
        t += dt
        let now = Date()
        if nextDeadline > now {
            Thread.sleep(forTimeInterval: nextDeadline.timeIntervalSince(now))
        }
    }
    let wall = Date().timeIntervalSince(wallStart)
    let cpu = processCPUTime() - cpuStart

    cpuSamples.sort()
    gpuSamples.sort()
    gpuSteady.sort()
    return BenchResult(
        state: state.rawValue,
        runKind: "gated",
        targetFPS: fps,
        sizePt: size,
        scale: scale,
        seconds: seconds,
        framesRendered: frames,
        achievedFPS: Double(frames) / wall,
        processCPUPct: 100 * cpu / max(wall, 0.001),
        frameCPUMeanMs: cpuSamples.isEmpty ? 0 : cpuSamples.reduce(0, +) / Double(cpuSamples.count),
        frameCPUP95Ms: percentile(cpuSamples, 0.95),
        frameGPUMeanMs: gpuSamples.isEmpty ? -1 : gpuSamples.reduce(0, +) / Double(gpuSamples.count),
        frameGPUMaxMs: gpuSamples.max() ?? -1,
        frameGPUSteadyMaxMs: gpuSteady.max() ?? -1,
        gpuSamples: gpuSamples.count
    )
}

/// Deployed-idle measurement: the link is STOPPED (no ticks at all); once a
/// second the host re-checks `linkShouldRun` (a state/level/visibility change
/// would restart it). Expect 0 frames and ~0% CPU.
func benchLinkStopped(state: OrbState, seconds: Double) -> BenchResult {
    let machine = OrbStateMachine()
    machine.setState(state)
    // Settle first (as the live host would before stopping the link).
    for _ in 0..<120 {
        _ = machine.update(dt: 1.0 / 60.0, inputLevel: 0, outputLevel: 0)
    }
    var violations = 0
    let wallStart = Date()
    let cpuStart = processCPUTime()
    let n = Int(seconds)
    for _ in 0..<n {
        Thread.sleep(forTimeInterval: 1.0)
        if FramePacingPolicy.linkShouldRun(
            state: machine.state, settled: true, levelActive: false, visible: true
        ) {
            violations += 1
        }
    }
    let wall = Date().timeIntervalSince(wallStart)
    let cpu = processCPUTime() - cpuStart
    return BenchResult(
        state: state.rawValue,
        runKind: "link-stopped",
        targetFPS: 0,
        sizePt: 0,
        scale: 2,
        seconds: seconds,
        framesRendered: violations,
        achievedFPS: 0,
        processCPUPct: 100 * cpu / max(wall, 0.001),
        frameCPUMeanMs: 0,
        frameCPUP95Ms: 0,
        frameGPUMeanMs: -1,
        frameGPUMaxMs: -1,
        frameGPUSteadyMaxMs: -1,
        gpuSamples: 0
    )
}
