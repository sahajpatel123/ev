import Atomics
import AVFoundation
import EVRuntime
import Foundation

// EVAudioHarness — minimal Mac audio baseline (directive §2/§3).
//
// Known-clean deterministic PCM → the EXACT production playback backend
// (PlaybackCoordinator: converter → SPSC ring → AVAudioSourceNode) → speakers.
// No G2, no camera, no HUD, no database, no network, no conversation logic.
//
// The PCM is a WRAP-FREE triangle ramp (rising (i mod 65536)-16384, then
// falling) so the production-side RT verifier can PROVE consumed-audio
// continuity with no discontinuity for the SRC filter to ring on: any missing,
// duplicated, reordered or corrupted frame is detected and counted at the last
// software boundary before the hardware. (The earlier sawtooth hid benign FIR
// ringing around its full-scale value step — position swings up to ±10975
// units — making verdicts unfalsifiable.)

enum Harness {
    static let outRate = 48_000.0
    static var globalSample: Int64 = 0
    static let stopFlag = ManagedAtomic<Bool>(false)

    // MARK: Deterministic PCM generation

    static func makeChunk(frames: Int) -> Data {
        var data = Data(count: frames * 2)
        data.withUnsafeMutableBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<frames {
                let idx = Int((globalSample + Int64(i)) % 65536)
                let v: Int32 = idx < 32768 ? Int32(idx) - 16384 : 49152 - Int32(idx)
                samples[i] = Int16(v)
            }
        }
        globalSample += Int64(frames)
        return data
    }

    static func writeWav16(path: String, pcm: Data, sampleRate: Int) throws {
        var out = Data()
        func str(_ s: String) { out.append(contentsOf: s.utf8) }
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { out.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { out.append(contentsOf: $0) } }
        str("RIFF"); u32(UInt32(36 + pcm.count)); str("WAVE")
        str("fmt "); u32(16); u16(1); u16(1); u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
        str("data"); u32(UInt32(pcm.count))
        out.append(pcm)
        try out.write(to: URL(fileURLWithPath: path))
    }

    // MARK: Resource monitors

    static func rssFootprintMB() -> Double {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard kr == KERN_SUCCESS else { return 0 }
        return Double(info.phys_footprint) / (1024 * 1024)
    }

    static func swapUsedMB() -> Double {
        var size = 0
        sysctlbyname("vm.swapusage", nil, &size, nil, 0)
        guard size > 0 else { return 0 }
        var buf = [CChar](repeating: 0, count: size)
        sysctlbyname("vm.swapusage", &buf, &size, nil, 0)
        let s = String(cString: buf)
        // "total = 2048.00M  used = 1461.00M  free = 587.00M"
        guard let range = s.range(of: "used = "),
              let end = s[range.upperBound...].firstIndex(of: "M") else { return 0 }
        return Double(s[range.upperBound..<end]) ?? 0
    }

    static func systemLoadAvg() -> Double {
        var loads = [Double](repeating: 0, count: 3)
        getloadavg(&loads, 3)
        return loads[0]
    }

    // MARK: Device listing

    static func listDevices() {
        let devs = PlaybackCoordinator.allDevices()
        for dev in devs {
            var cfName: CFString?
            var size = UInt32(MemoryLayout<CFString?>.size)
            var addr = AudioObjectPropertyAddress(
                mSelector: kAudioObjectPropertyName,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            var name = "?"
            if AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &cfName) == noErr, let cfName {
                name = cfName as String
            }
            var t: UInt32 = 0
            size = UInt32(MemoryLayout<UInt32>.size)
            addr.mSelector = kAudioDevicePropertyTransportType
            if AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &t) == noErr {
                let transport: String
                switch t {
                case kAudioDeviceTransportTypeBuiltIn: transport = "built-in"
                case kAudioDeviceTransportTypeBluetooth: transport = "bluetooth"
                case kAudioDeviceTransportTypeUSB: transport = "usb"
                default: transport = "type(\(t))"
                }
                _ = transport
            }
            var cfUid: CFString?
            size = UInt32(MemoryLayout<CFString?>.size)
            addr.mSelector = kAudioDevicePropertyDeviceUID
            var uid = "?"
            if AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &cfUid) == noErr, let cfUid {
                uid = cfUid as String
            }
            var hasOut = false
            size = 0
            addr.mSelector = kAudioDevicePropertyStreams
            addr.mScope = kAudioObjectPropertyScopeOutput
            if AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 { hasOut = true }
            var nominal: Double = 0
            size = UInt32(MemoryLayout<Double>.size)
            addr.mSelector = kAudioDevicePropertyNominalSampleRate
            addr.mScope = kAudioObjectPropertyScopeGlobal
            if AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &nominal) != noErr { }
            print("device '\(name)' output=\(hasOut) rate=\(nominal) uid=\(uid)")
        }
    }

    // MARK: Response runner

    struct ResponseResult {
        let index: Int
        let durationSec: Double
        let pass: Bool
        let missingEvents: Int64
        let missingFrames: Int64
        let dupEvents: Int64
        let silenceFrames: Int64
        let stretchEvents: Int64
        let overruns: Int64
        let underrunFrames: Int64
        let peakBacklogMs: Double
        let maxAgeMs: Double
        let renderedMs: Double
        let expectedMs: Double
    }

    static func waitForDrain(expectedOutFrames: Int64, timeout: TimeInterval) async -> (drained: Bool, peakBacklogMs: Double, maxAgeMs: Double) {
        var peakBacklogMs = 0.0
        var maxAgeMs = 0.0
        let start = Date()
        var zeroStreak = 0
        while Date().timeIntervalSince(start) < timeout {
            let snap = await PlaybackCoordinator.shared.snapshot()
            let bufferedMs = snap["bufferedMs"] as? Double ?? 0
            let age = snap["oldestAgeMs"] as? Double ?? 0
            peakBacklogMs = max(peakBacklogMs, bufferedMs)
            maxAgeMs = max(maxAgeMs, age)
            let rendered = snap["diagRenderedFrames"] as? Int64 ?? snap["totalReads"] as? Int64 ?? 0
            // 96-frame (2ms) tolerance: polyphase SRC may legitimately land
            // 1-3 output frames short of the ideal 3.0x expansion.
            if bufferedMs <= 0 && rendered >= expectedOutFrames - 96 {
                zeroStreak += 1
                if zeroStreak >= 2 { return (true, peakBacklogMs, maxAgeMs) }
            } else {
                zeroStreak = 0
            }
            try? await Task.sleep(nanoseconds: 40_000_000)
        }
        return (false, peakBacklogMs, maxAgeMs)
    }

    static func streamResponse(durationSec: Double, rate: Double, chunkMsMin: Double, chunkMsMax: Double, rng: inout SplitMix64) async {
        var produced = 0.0
        var carry = Data()
        while produced < durationSec {
            let chunkMs = chunkMsMin + rng.nextDouble() * (chunkMsMax - chunkMsMin)
            let frames = min(Int(chunkMs * 16), Int((durationSec - produced) * 16000))
            guard frames > 0 else { break }
            carry = makeChunk(frames: frames)
            let t0 = DispatchTime.now().uptimeNanoseconds
            await PlaybackCoordinator.shared.enqueue(pcm: carry, sampleRate: 16000)
            produced += Double(frames) / 16000.0
            // Pacing: chunk duration / rate, minus the time enqueue took.
            let targetNs = UInt64(Double(frames) / 16000.0 / rate * 1_000_000_000)
            var slept: UInt64 = 0
            while slept < targetNs {
                let step = min(2_000_000, targetNs - slept)
                usleep(UInt32(step / 1000))
                slept += step
                if stopFlag.load(ordering: .relaxed) { return }
            }
            _ = t0
        }
    }

    static func runResponse(index: Int, durationSec: Double, rate: Double, chunkMsMin: Double, chunkMsMax: Double, rng: inout SplitMix64) async -> ResponseResult {
        let expectedOutFrames = Int64(durationSec * 16000.0 * 3.0)
        let pre = await PlaybackCoordinator.shared.snapshot()
        await streamResponse(durationSec: durationSec, rate: rate, chunkMsMin: chunkMsMin, chunkMsMax: chunkMsMax, rng: &rng)
        let (drained, peakBacklogMs, maxAgeMs) = await waitForDrain(expectedOutFrames: expectedOutFrames, timeout: max(10, durationSec * 2))
        let snap = await PlaybackCoordinator.shared.snapshot()
        func d(_ key: String) -> Int64 {
            (snap[key] as? Int64 ?? 0) - (pre[key] as? Int64 ?? 0)
        }
        let missingEvents = d("diagMissingEvents")
        let missingFrames = d("diagMissingFrames")
        let dupEvents = d("diagDupEvents")
        let silenceFrames = d("diagSilenceFrames")
        let stretch = d("diagStretchEvents")
        let overruns = d("overrunCount")
        let underruns = d("underrunFrames")
        let rendered = d("diagRenderedFrames")
        let renderedMs = Double(rendered) / 48.0
        let expectedMs = durationSec * 1000
        let pass = drained
            && missingEvents == 0 && missingFrames == 0
            && dupEvents == 0
            && silenceFrames == 0
            && overruns == 0
            && underruns == 0
            && abs(renderedMs - expectedMs) < max(60.0, expectedMs * 0.01)
        return ResponseResult(
            index: index, durationSec: durationSec, pass: pass,
            missingEvents: missingEvents, missingFrames: missingFrames,
            dupEvents: dupEvents, silenceFrames: silenceFrames, stretchEvents: stretch,
            overruns: overruns, underrunFrames: underruns,
            peakBacklogMs: peakBacklogMs, maxAgeMs: maxAgeMs,
            renderedMs: renderedMs, expectedMs: expectedMs
        )
    }

    // MARK: Load generators

    static func startCpuLoad(threads: Int) -> Thread {
        let t = Thread {
            while !stopFlag.load(ordering: .relaxed) {
                var x: UInt64 = 0
                let deadline = Date().addingTimeInterval(0.08)
                while Date() < deadline { x = x &* 6364136223846793005 &+ 1442695040888963407 }
                if x == 1 { print("") }
                usleep(20_000)
            }
        }
        t.qualityOfService = .userInitiated
        for _ in 0..<max(1, threads - 1) {
            let extra = Thread {
                while !stopFlag.load(ordering: .relaxed) {
                    var x: UInt64 = 0
                    let deadline = Date().addingTimeInterval(0.08)
                    while Date() < deadline { x = x &* 6364136223846793005 &+ 1442695040888963407 }
                    if x == 1 { print("") }
                    usleep(20_000)
                }
            }
            extra.qualityOfService = .userInitiated
            extra.start()
        }
        t.start()
        return t
    }

    static func startMemLoad(mb: Int) -> [UnsafeMutablePointer<UInt8>] {
        var blocks: [UnsafeMutablePointer<UInt8>] = []
        for _ in 0..<mb {
            let count = 1_000_000
            let p = UnsafeMutablePointer<UInt8>.allocate(capacity: count)
            for i in stride(from: 0, to: count, by: 4096) { p[i] = 1 }
            blocks.append(p)
        }
        return blocks
    }

    struct SplitMix64 {
        var state: UInt64
        mutating func next() -> UInt64 {
            state = state &+ 0x9E3779B97F4A7C15
            var z = state
            z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
            z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
            return z ^ (z >> 31)
        }
        mutating func nextDouble() -> Double { Double(next() >> 11) / Double(1 << 53) }
    }
}

@main
struct Main {
    static func argValue(_ name: String) -> String? {
        guard let i = CommandLine.arguments.firstIndex(of: name), i + 1 < CommandLine.arguments.count else { return nil }
        return CommandLine.arguments[i + 1]
    }

    static func main() async {
        let args = CommandLine.arguments
        if args.contains("--list-devices") {
            Harness.listDevices()
            return
        }
        let stage = argValue("--stage") ?? "A"
        let rate = Double(argValue("--rate") ?? "1.0") ?? 1.0
        let chunkRange = (argValue("--chunk-ms") ?? "40:120").split(separator: ":").compactMap { Double($0) }
        let chunkMsMin = chunkRange.count == 2 ? chunkRange[0] : 40
        let chunkMsMax = chunkRange.count == 2 ? chunkRange[1] : 120
        let loadCpu = Int(argValue("--load-cpu") ?? "0") ?? 0
        let loadMemMB = Int(argValue("--load-mem") ?? "0") ?? 0
        let outDir = argValue("--out-dir") ?? "/tmp/ev-audio-harness"
        var rng = Harness.SplitMix64(state: 0xEF01CE)

        print("== EVAudioHarness ==")
        print("stage=\(stage) rate=\(rate) chunkMs=\(chunkMsMin)-\(chunkMsMax) loadCpu=\(loadCpu) loadMemMB=\(loadMemMB)")

        try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

        // P0 reference: the exact PCM we will feed, as a WAV for forensics.
        do {
            let refFrames = 16_000 * 5
            let pcm = Harness.makeChunk(frames: refFrames)
            try Harness.writeWav16(path: outDir + "/p0_reference_16k.wav", pcm: pcm, sampleRate: 16000)
        } catch {
            print("WARN: could not write P0 reference: \(error)")
        }

        var loadThreads: Thread?
        var memBlocks: [UnsafeMutablePointer<UInt8>] = []
        if loadCpu > 0 { loadThreads = Harness.startCpuLoad(threads: loadCpu) }
        if loadMemMB > 0 { memBlocks = Harness.startMemLoad(mb: loadMemMB) }

        let pressureEvent = ManagedAtomic<Int64>(0)
        let mp = DispatchSource.makeMemoryPressureSource(eventMask: [.warning, .critical], queue: .global(qos: .utility))
        mp.setEventHandler { pressureEvent.wrappingIncrement(ordering: .relaxed) }
        mp.resume()

        var responses: [(dur: Double, count: Int)] = {
            switch stage.uppercased() {
            case "A": return Array(repeating: 0.0, count: 20).enumerated().map { (2.0 + Double($0.offset % 7), 1) }
            case "B": return (0..<10).map { (10.0 + Double($0), 1) }
            case "C": return [30.0, 37.0, 44.0, 51.0, 58.0].map { ($0, 1) }
            default:
                let secs = Double(argValue("--seconds") ?? "5") ?? 5
                return [(secs, 1)]
            }
        }()

        var results: [Harness.ResponseResult] = []
        var idx = 0
        let runStart = Date()
        var nextMarkIdx = 0
        let marks = [5.0, 10.0, 20.0, 30.0, 60.0]
        for (dur, count) in responses {
            for _ in 0..<count {
                idx += 1
                print("response #\(idx): \(Int(dur))s …")
                let r = await Harness.runResponse(index: idx, durationSec: dur, rate: rate, chunkMsMin: chunkMsMin, chunkMsMax: chunkMsMax, rng: &rng)
                results.append(r)
                print(String(
                    format: "  -> %@ rendered=%.0fms expected=%.0fms missing=%lld dup=%lld silence=%lldms stretch=%lld overrun=%lld underrun=%lldms peakBacklog=%.0fms maxAge=%.0fms",
                    r.pass ? "PASS" : "FAIL",
                    r.renderedMs, r.expectedMs,
                    r.missingEvents, r.dupEvents, r.silenceFrames / 48, r.stretchEvents,
                    r.overruns, r.underrunFrames / 48, r.peakBacklogMs, r.maxAgeMs
                ))
                // Queue-age marks during conversation (§11)
                let elapsed = Date().timeIntervalSince(runStart)
                while nextMarkIdx < marks.count, elapsed >= marks[nextMarkIdx] {
                    let snap = await PlaybackCoordinator.shared.snapshot()
                    print(String(format: "  age@%ds: oldest=%.0fms buffered=%.0fms", Int(marks[nextMarkIdx]), snap["oldestAgeMs"] as? Double ?? 0, snap["bufferedMs"] as? Double ?? 0))
                    nextMarkIdx += 1
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }

        let snap = await PlaybackCoordinator.shared.snapshot()
        let passes = results.filter(\.pass).count
        print("\n== STAGE \(stage) RESULT: \(passes)/\(results.count) clear ==")
        print("RSS(footprint)MB=\(String(format: "%.0f", Harness.rssFootprintMB())) swapUsedMB=\(String(format: "%.0f", Harness.swapUsedMB())) load1=\(String(format: "%.2f", Harness.systemLoadAvg())) pressureEvents=\(pressureEvent.load(ordering: .relaxed))")
        print("engine hwDevice=\(snap["hwDeviceName"] ?? "?") hwRate=\(snap["hwDeviceRate"] ?? 0) hwBufferFrames=\(snap["hwBufferFrames"] ?? 0) engineRunning=\(snap["engineRunning"] ?? false)")
        print("cb p50=\(snap["diagCbP50Us"] ?? -1)us p95=\(snap["diagCbP95Us"] ?? -1)us p99=\(snap["diagCbP99Us"] ?? -1)us max=\(snap["diagCbMaxUs"] ?? -1)us callbacks=\(snap["diagCallbacks"] ?? 0)")
        print("totals: missingEvents=\(snap["diagMissingEvents"] ?? -1) missingFrames=\(snap["diagMissingFrames"] ?? -1) dup=\(snap["diagDupEvents"] ?? -1) silenceFrames=\(snap["diagSilenceFrames"] ?? -1) underrunFrames=\(snap["underrunFrames"] ?? -1) overruns=\(snap["overrunCount"] ?? -1) stallRestarts=\(snap["stallRestarts"] ?? 0)")
        print("diag dir: \(ProcessInfo.processInfo.environment["EV_AUDIO_DIAG_DIR"] ?? "/tmp/ev-audio-diag")")
        Harness.stopFlag.store(true, ordering: .relaxed)
        memBlocks.forEach { $0.deallocate() }
        exit(passes == results.count ? 0 : 1)
    }
}
