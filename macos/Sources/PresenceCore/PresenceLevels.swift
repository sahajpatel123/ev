import Combine
import Foundation

/// Local-only audio levels for the orb (CLI 3, branch evie-orb).
///
/// The orb is audio-reactive at animation rate (~30 Hz). Those levels are
/// published HERE, on-device, and NEVER traverse the presence event stream
/// (contract §"Audio levels" note): the stream's `turn.listening.level` is
/// only a coarse activity hint for state display.
///
/// Sources (wired at integration):
/// - input: `turn.listening` events (coarse) until the mic tap lands, then
///   the live capture meter.
/// - output: the TTS playback meter (silent when Evie is not speaking).
/// Until a source is wired, each level rests at 0 — never a fake wave.
@MainActor
public final class PresenceLevels: ObservableObject {
    /// Mic input level, 0..1. Coarse until the live tap is wired.
    @Published public private(set) var inputLevel: Double = 0
    /// Playback output level, 0..1. 0 unless Evie is speaking.
    @Published public private(set) var outputLevel: Double = 0

    public init() {}

    public func updateInput(_ level: Double) {
        inputLevel = min(1, max(0, level))
    }

    public func updateOutput(_ level: Double) {
        outputLevel = min(1, max(0, level))
    }

    public func reset() {
        inputLevel = 0
        outputLevel = 0
    }
}
