import AVFoundation
import Foundation

/// Dedicated playback authority: owns the output audio graph and jitter buffer.
/// All hot-path PCM ingestion and scheduling runs on this actor's executor,
/// never on MainActor, so G2 polling, UI, or mic recovery cannot starve it.
actor PlaybackCoordinator {
    static let shared = PlaybackCoordinator()

    // Use the AppModel's single TTSPlayer so UI (isPlaying, onPlayingChange)
    // and the dedicated audio path share the same engine and queue depth.
    // Set once from AppModel.attach on MainActor.
    nonisolated(unsafe) private var tts: TTSPlayer?

    func setPlayer(_ player: TTSPlayer) {
        tts = player
    }

    func enqueue(pcm: Data, sampleRate: Double) {
        guard let tts else { return }
        // Forward to TTSPlayer but on this actor's executor, not MainActor.
        try? tts.enqueue(pcm, contentType: "audio/pcm", sampleRate: sampleRate)
    }

    func enqueueFromBase64(_ b64: String, sampleRate: Double) {
        guard let data = Data(base64Encoded: b64), let tts else { return }
        try? tts.enqueue(data, contentType: "audio/pcm", sampleRate: sampleRate)
    }

    func stop() {
        tts?.stop()
    }

    var isPlaying: Bool { tts?.isPlaying ?? false }
    var pendingFrames: Int { tts?.pendingFramesPublic ?? 0 }
}
