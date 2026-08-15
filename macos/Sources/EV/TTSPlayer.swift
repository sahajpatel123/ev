import AVFoundation
import Foundation

/// Plays TTS audio returned by Agent 4. Chunks enqueue so first-word audio
/// can start before the rest of the reply arrives.
final class TTSPlayer: NSObject, AVAudioPlayerDelegate {
    private var player: AVAudioPlayer?
    private var queue: [Data] = []
    var onPlayingChange: ((Bool) -> Void)?

    var isPlaying: Bool {
        player?.isPlaying ?? false
    }

    func play(data: Data) throws {
        queue.removeAll()
        try playNow(data)
    }

    func enqueue(_ data: Data) throws {
        if player?.isPlaying == true {
            queue.append(data)
            return
        }
        try playNow(data)
    }

    func stop() {
        let wasPlaying = isPlaying || !queue.isEmpty
        queue.removeAll()
        player?.delegate = nil
        player?.stop()
        player = nil
        if wasPlaying {
            onPlayingChange?(false)
        }
    }

    private func playNow(_ data: Data) throws {
        let wasPlaying = isPlaying
        player?.delegate = nil
        player?.stop()
        let next = try AVAudioPlayer(data: data)
        next.delegate = self
        next.prepareToPlay()
        next.play()
        player = next
        if !wasPlaying {
            onPlayingChange?(true)
        }
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        if !queue.isEmpty {
            let next = queue.removeFirst()
            try? playNow(next)
            return
        }
        self.player = nil
        onPlayingChange?(false)
    }
}
