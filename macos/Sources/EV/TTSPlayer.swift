import AVFoundation
import Foundation

/// Plays TTS audio returned by Agent 4 as `tts.audio_ref` bytes.
final class TTSPlayer {
    private var player: AVAudioPlayer?

    var isPlaying: Bool {
        player?.isPlaying ?? false
    }

    func play(data: Data) throws {
        player?.stop()
        let player = try AVAudioPlayer(data: data)
        player.prepareToPlay()
        player.play()
        self.player = player
    }

    func stop() {
        player?.stop()
        player = nil
    }
}
