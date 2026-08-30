import EVRuntime
import Foundation

/// Drive values for the speech-response orb.
struct VoiceOrbEnergy {
    var status: VoiceOrbSpeechStatus
    var audioLevel: Float

    static func resolve(
        status: AppModel.Status,
        muted: Bool,
        live: Bool,
        input: Float,
        output: Float,
        time: TimeInterval
    ) -> VoiceOrbEnergy {
        _ = muted
        _ = live
        _ = input
        _ = time
        let speech = VoicePresenceMath.speechStatus(forAppStatus: status.rawValue)
        return VoiceOrbEnergy(
            status: speech,
            audioLevel: VoicePresenceMath.speechAudioLevel(status: speech, output: output)
        )
    }
}
