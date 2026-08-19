import EVRuntime
import Foundation

/// Smoothed microphone and TTS energy for the presence orb.
///
/// Fed from the existing live capture tap and TTS PCM path — never from a
/// second `AVAudioEngine`. The orb polls `snapshot()` on the display clock.
final class VoiceLevelMeter: @unchecked Sendable {
    static let shared = VoiceLevelMeter()

    private let lock = NSLock()
    private var input: Float = 0
    private var output: Float = 0
    private var lastInputAt = Date.distantPast
    private var lastOutputAt = Date.distantPast

    private init() {}

    func ingestInputPCM16(_ data: Data) {
        ingest(VoicePresenceMath.normalizeSpeechRMS(VoicePresenceMath.pcm16RMS(data)), input: true)
    }

    func ingestOutputPCM16(_ data: Data) {
        ingest(VoicePresenceMath.normalizeSpeechRMS(VoicePresenceMath.pcm16RMS(data)), input: false)
    }

    func resetInput() {
        lock.lock()
        input = 0
        lastInputAt = Date.distantPast
        lock.unlock()
    }

    func snapshot() -> (input: Float, output: Float) {
        lock.lock()
        defer { lock.unlock() }
        let now = Date()
        if now.timeIntervalSince(lastInputAt) > 0.09 {
            input *= 0.78
            if input < 0.01 { input = 0 }
        }
        if now.timeIntervalSince(lastOutputAt) > 0.09 {
            output *= 0.78
            if output < 0.01 { output = 0 }
        }
        return (input, output)
    }

    private func ingest(_ sample: Float, input isInput: Bool) {
        lock.lock()
        if isInput {
            input = VoicePresenceMath.smooth(previous: input, sample: sample)
            lastInputAt = Date()
        } else {
            output = VoicePresenceMath.smooth(previous: output, sample: sample, attack: 0.38, release: 0.18)
            lastOutputAt = Date()
        }
        lock.unlock()
    }
}
