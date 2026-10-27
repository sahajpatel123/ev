import EvieOrb
import Foundation

/// Minimal headless check framework (repo idiom: `swift run EvieOrbCheck`).
/// Mirrors EVClientCheck / EVMicTalkTests: plain asserts, non-zero exit.
struct CheckContext {
    var failures: [String] = []
    var passes: Int = 0

    mutating func expect(_ condition: Bool, _ message: String, file: String = #fileID, line: Int = #line) {
        if condition {
            passes += 1
        } else {
            failures.append("\(file):\(line): \(message)")
        }
    }
}

/// Tiny deterministic RNG shared by checks (xorshift64).
struct SeededRNG {
    var state: UInt64
    init(seed: UInt64) { state = seed == 0 ? 1 : seed }
    mutating func next() -> Int {
        var x = state
        x ^= x >> 12
        x ^= x << 25
        x ^= x >> 27
        state = x
        return Int(truncatingIfNeeded: UInt64((x &* 0x2545F4914F6CDD1D) >> 33))
    }
}

var ctx = CheckContext()
let filter = CommandLine.arguments.dropFirst().first

func run(_ name: String, _ body: (inout CheckContext) -> Void) {
    if let filter, !name.contains(filter) { return }
    let before = ctx.failures.count
    body(&ctx)
    let newFailures = ctx.failures.count - before
    print("\(newFailures == 0 ? "PASS" : "FAIL") \(name)")
}

run("EnvelopeChecks", runEnvelopeChecks)
run("StateMachineChecks", runStateMachineChecks)
run("LevelSourceChecks", runLevelSourceChecks)
run("MappingChecks", runMappingChecks)
run("LayoutChecks", runLayoutChecks)
run("HostChecks", runHostChecks)
run("PacingChecks", runPacingChecks)
run("RendererChecks", runRendererChecks)

print("---")
print("passes: \(ctx.passes) failures: \(ctx.failures.count)")
for f in ctx.failures { print("FAILURE: \(f)") }
exit(ctx.failures.isEmpty ? 0 : 1)
