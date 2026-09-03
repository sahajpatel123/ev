import Foundation

/// Which TTS player lane a live `tts_chunk` belongs to.
///
/// Greeting PCM can start before Realtime assigns a response id; adopting that
/// id must not chop the first word. A later *different* id is a new Realtime
/// response (tool continuation). If the player is starved, that audio must
/// play via a fresh response; otherwise it is a continuation of the same
/// spoken turn and must NOT be dropped or it glitches the sentence.
public enum LivePlaybackLane {
    public enum Decision: Equatable, Sendable {
        case enqueue
        case adoptProviderId
        case rollToNewResponse
        case drop
    }

    public static func decide(
        acceptedProviderId: String?,
        incomingProviderId: String?,
        queuedFrames: Int
    ) -> Decision {
        let incoming = incomingProviderId?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if incoming.isEmpty { return .enqueue }
        let accepted = acceptedProviderId?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if accepted.isEmpty { return .adoptProviderId }
        if accepted == incoming { return .enqueue }
        if queuedFrames <= 0 { return .rollToNewResponse }
        return .adoptProviderId
    }
}
