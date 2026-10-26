import EvieOrb
import Foundation

/// R3: level DSP correctness, atomic publish, synthetic source determinism.
func runLevelSourceChecks(_ c: inout CheckContext) {
    func pcm16Data(samples: [Int16]) -> Data {
        var data = Data(capacity: samples.count * 2)
        for var s in samples {
            withUnsafeBytes(of: &s) { data.append(contentsOf: $0) }
        }
        return data
    }

    do {
        var rng = SeededRNG(seed: 42)
        var samples = [Int16]()
        for _ in 0..<2048 {
            samples.append(Int16(rng.next() % 20000) - 10000)
        }
        let data = pcm16Data(samples: samples)
        let got = OrbLevelDSP.rmsPCM16(data)
        var sum: Double = 0
        for s in samples { sum += Double(s) * Double(s) }
        let want = Float(sqrt(sum / Double(samples.count)))
        c.expect(abs(got - want) <= max(1.0, want * 0.001), "vDSP RMS \(got) != naive \(want)")
    }

    c.expect(OrbLevelDSP.rmsPCM16(Data(count: 4096)) == 0, "silence RMS != 0")
    c.expect(OrbLevelDSP.rmsPCM16(Data()) == 0, "empty RMS != 0")

    c.expect(OrbLevelDSP.normalize(rms: 0) == 0, "normalize(0) != 0")
    c.expect(OrbLevelDSP.normalize(rms: 40) == 0, "gate leaked at rms=40")
    c.expect(OrbLevelDSP.normalize(rms: 3600) > 0.5, "speech rms maps too low")
    c.expect(OrbLevelDSP.normalize(rms: 30000) <= 1.0, "normalize clips above 1")

    do {
        let meter = AtomicOrbLevelMeter()
        var samples = [Int16]()
        for i in 0..<1600 {
            samples.append(Int16(8000 * sin(2 * .pi * Double(i) / 16)))
        }
        meter.ingestInputPCM16(pcm16Data(samples: samples))
        c.expect(meter.inputLevel > 0.3, "input level too low (\(meter.inputLevel))")
        c.expect(meter.outputLevel == 0, "input leaked to output")
        meter.ingestOutputPCM16(pcm16Data(samples: samples))
        c.expect(meter.outputLevel > 0.3, "output level too low (\(meter.outputLevel))")
    }

    do {
        let meter = AtomicOrbLevelMeter()
        meter.ingestInputPCM16(pcm16Data(samples: [Int16](repeating: 8000, count: 1600)))
        c.expect(meter.inputLevel > 0, "meter never rose")
        let silent = pcm16Data(samples: [Int16](repeating: 0, count: 1600))
        for _ in 0..<40 {
            Thread.sleep(forTimeInterval: 0.01)
            meter.ingestInputPCM16(silent)
        }
        c.expect(meter.inputLevel < 0.2, "meter never decayed (\(meter.inputLevel))")
    }

    do {
        let a = SyntheticLevelSource(seed: 7)
        let b = SyntheticLevelSource(seed: 7)
        var same = true
        for _ in 0..<120 {
            a.advance(by: 1.0 / 60.0)
            b.advance(by: 1.0 / 60.0)
            same = same && a.inputLevel == b.inputLevel && a.outputLevel == b.outputLevel
        }
        c.expect(same, "synthetic source not deterministic")
    }

    do {
        let src = SyntheticLevelSource(seed: 1, profile: .speech)
        var minV: Float = 1, maxV: Float = 0
        for _ in 0..<240 {
            src.advance(by: 1.0 / 60.0)
            minV = min(minV, src.inputLevel)
            maxV = max(maxV, src.inputLevel)
        }
        c.expect(maxV > 0.35, "synthetic speech never loud (\(maxV))")
        c.expect(minV < 0.2, "synthetic speech never pauses (\(minV))")
    }
}
