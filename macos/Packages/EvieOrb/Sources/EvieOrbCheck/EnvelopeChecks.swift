import EvieOrb
import Foundation

/// R2: envelope follower — fast attack, slow release, perceptual scaling, gate.
func runEnvelopeChecks(_ c: inout CheckContext) {
    // Attack: 0 -> 1 step, tau = 30 ms => ~63% after 30 ms of 1 ms steps.
    var env = EnvelopeFollower()
    var v: Float = 0
    for _ in 0..<30 { v = env.update(sample: 1.0, dt: 0.001) }
    c.expect(v > 0.55 && v < 0.75, "attack 30ms gave \(v), want ~0.63")

    // Release: 1 -> 0 step, tau = 250 ms.
    env = EnvelopeFollower()
    for _ in 0..<300 { _ = env.update(sample: 1.0, dt: 0.001) }
    let peak = env.current
    c.expect(peak > 0.95, "never reached peak (\(peak))")
    for _ in 0..<30 { v = env.update(sample: 0.0, dt: 0.001) }
    c.expect(v > peak * 0.75, "release too fast: \(v) after 30ms from \(peak)")
    for _ in 0..<1500 { v = env.update(sample: 0.0, dt: 0.001) }
    c.expect(v < 0.02, "release never settled (\(v))")

    // Noise gate: constant low hum stays at zero.
    env = EnvelopeFollower()
    for _ in 0..<500 { v = env.update(sample: 0.008, dt: 0.004) }
    c.expect(abs(v) <= 0.0001, "gate leaked hum: \(v)")

    // Speech above gate passes.
    env = EnvelopeFollower()
    for _ in 0..<500 { v = env.update(sample: 0.4, dt: 0.004) }
    c.expect(v > 0.3, "speech blocked by gate: \(v)")

    // Perceptual scaling: half input maps well above half output.
    env = EnvelopeFollower()
    for _ in 0..<2000 { _ = env.update(sample: 0.5, dt: 0.004) }
    c.expect(env.current > 0.6, "log scaling missing: \(env.current)")

    // Syllable-rate AM breathes: rises and falls, never sticks.
    env = EnvelopeFollower()
    var minV: Float = 1, maxV: Float = 0
    for i in 0..<600 {
        let t = Double(i) * 0.004
        let syllable = max(0, sin(2 * .pi * 4 * t))
        let pause = (t.truncatingRemainder(dividingBy: 1.2) > 0.8) ? 0.0 : 1.0
        let sample = Float(syllable * pause * 0.8)
        let vv = env.update(sample: sample, dt: 0.004)
        minV = min(minV, vv)
        maxV = max(maxV, vv)
    }
    c.expect(maxV > 0.4, "envelope never rose (\(maxV))")
    c.expect(minV < 0.15, "envelope never fell (\(minV))")

    env = EnvelopeFollower()
    for _ in 0..<100 { _ = env.update(sample: 1.0, dt: 0.004) }
    env.reset()
    c.expect(env.current == 0, "reset did not clear")
}
