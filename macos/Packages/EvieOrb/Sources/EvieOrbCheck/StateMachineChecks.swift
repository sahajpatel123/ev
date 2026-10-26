import EvieOrb
import Foundation

/// R2: state machine — smooth critically damped transitions, momentary states.
func runStateMachineChecks(_ c: inout CheckContext) {
    /// Drive at 60 fps; return largest single per-frame param jump (snap detector).
    func drive(
        _ machine: OrbStateMachine, seconds: Double,
        inputLevel: Float = 0, outputLevel: Float = 0
    ) -> Float {
        let dt = 1.0 / 60.0
        var prev = machine.current
        var maxJump: Float = 0
        for _ in 0..<Int(seconds / dt) {
            let p = machine.update(dt: dt, inputLevel: inputLevel, outputLevel: outputLevel)
            for jump in OrbParameters.diffMagnitude(prev, p) {
                maxJump = max(maxJump, jump)
            }
            prev = p
        }
        return maxJump
    }

    for from in OrbState.allCases {
        for to in OrbState.allCases where to != from {
            let machine = OrbStateMachine()
            machine.setState(from)
            _ = drive(machine, seconds: 1.0)
            machine.setState(to)
            let jump = drive(machine, seconds: 1.0)
            c.expect(jump < 0.25, "snap in \(from) -> \(to): jump \(jump)")
        }
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.thinking)
        _ = drive(machine, seconds: 2.0)
        let settled = machine.current
        let target = OrbParameters.target(for: .thinking, level: 0, reduceMotion: false, appearance: .dark)
        for jump in OrbParameters.diffMagnitude(settled, target) {
            c.expect(jump < 0.05, "thinking did not settle (residual \(jump))")
        }
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.listening)
        _ = drive(machine, seconds: 1.0, inputLevel: 0.05)
        let quietEnergy = machine.current.energy
        _ = drive(machine, seconds: 1.0, inputLevel: 0.9)
        let loudEnergy = machine.current.energy
        c.expect(loudEnergy > quietEnergy + 0.2, "listening ignores level (\(quietEnergy) -> \(loudEnergy))")
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.speaking)
        _ = drive(machine, seconds: 1.0, inputLevel: 0.9, outputLevel: 0.05)
        let energyOnInputOnly = machine.current.energy
        _ = drive(machine, seconds: 1.0, inputLevel: 0.0, outputLevel: 0.9)
        let energyOnOutput = machine.current.energy
        c.expect(
            energyOnOutput > energyOnInputOnly + 0.2,
            "speaking follows wrong channel (\(energyOnInputOnly) vs \(energyOnOutput))"
        )
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.success)
        c.expect(machine.state == .success, "success not entered")
        _ = drive(machine, seconds: 2.5)
        c.expect(machine.state == .idle, "success did not decay (still \(machine.state))")
        c.expect(machine.current.bloom < 0.05, "bloom stuck at \(machine.current.bloom)")
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.error)
        _ = drive(machine, seconds: 2.0)
        c.expect(machine.state == .idle, "error did not decay (still \(machine.state))")
        c.expect(machine.current.coolDim < 0.05, "coolDim stuck at \(machine.current.coolDim)")
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.needsConfirm)
        _ = drive(machine, seconds: 5.0)
        c.expect(machine.state == .needsConfirm, "needsConfirm not sticky")
        c.expect(machine.current.confirmPulse > 0.5, "confirm pulse missing")
    }

    do {
        let machine = OrbStateMachine(reduceMotion: true)
        machine.setState(.thinking)
        _ = drive(machine, seconds: 1.0, inputLevel: 0.8)
        c.expect(machine.current.flowSpeed == 0, "reduceMotion flow not frozen")
        c.expect(machine.current.rotationSpeed == 0, "reduceMotion rotation not frozen")
        c.expect(machine.current.energy > 0.05, "reduceMotion killed brightness")
    }

    do {
        let machine = OrbStateMachine()
        machine.setState(.offline)
        _ = drive(machine, seconds: 1.5)
        let p = machine.current
        c.expect(p.thickness < 0.05, "offline not thin (\(p.thickness))")
        c.expect(p.opacity < 0.5, "offline not dim (\(p.opacity))")
        c.expect(p.flowSpeed == 0, "offline not static")
    }

    do {
        let idleT = OrbParameters.target(for: .idle, level: 0, reduceMotion: false, appearance: .dark)
        let thinkT = OrbParameters.target(for: .thinking, level: 0, reduceMotion: false, appearance: .dark)
        c.expect(thinkT.radius < idleT.radius, "thinking not tighter")
        c.expect(thinkT.flowSpeed < 0.6, "thinking flow too fast")
        c.expect(thinkT.rotationSpeed > idleT.rotationSpeed, "thinking does not rotate")
    }

    do {
        let actingT = OrbParameters.target(for: .acting, level: 0, reduceMotion: false, appearance: .dark)
        c.expect(actingT.orbitStrength > 0.5, "acting orbit missing")
    }
}
