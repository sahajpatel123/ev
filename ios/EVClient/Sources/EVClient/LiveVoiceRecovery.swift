import Foundation

/// The client-side action for a server-reported live voice failure.
///
/// ASR is intentionally a backend concern, but the websocket client still
/// owns a small piece of recovery policy: a failed native speech stream must
/// not leave the UI in ``thinking`` forever or keep feeding a dead channel.
/// This type stays provider-neutral so the same behavior applies to Muse,
/// another hosted recognizer, or a future local recognizer.
public enum LiveVoiceRecoveryAction: Sendable, Equatable {
    /// No turn or transport state needs changing.
    case none
    /// Discard the incomplete response latch and return to listening.
    case resetTurn
    /// Close this live channel so the owning lifecycle can reconnect once.
    case reconnect
}

public enum LiveVoiceRecoveryPolicy {
    /// Classify only non-fatal live errors. Fatal events already flow through
    /// the normal connection lifecycle (and may intentionally mean sleep or
    /// explicit session termination).
    public static func action(for event: LiveVoiceEvent) -> LiveVoiceRecoveryAction {
        guard event.type == "error", !event.fatal else { return .none }

        let code = (event.code ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let message = (event.text ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()

        // Empty/no-speech is an ordinary turn outcome, not a broken stream.
        if code == "asr_empty_result" || code == "asr_no_speech" {
            return .none
        }

        // A native stream that closed, timed out, or became unusable cannot
        // receive another PCM turn. Reopening the EV live channel gives the
        // backend a fresh ASR session. The caller gates this to one request
        // per connection generation so a persistent provider outage cannot
        // create a client-side reconnect storm.
        if code == "asr_connection_closed"
            || code == "asr_timeout"
            || code == "asr_unusable"
            || code == "asr_stream_failed"
            || message.contains("muse voice transcribe reported an error") {
            return .reconnect
        }

        // Authentication, quota, and format errors are deterministic for the
        // current configuration. Reset the pending turn and keep the socket
        // available for controls/text while the backend reports the problem.
        if code.hasPrefix("asr_") || message.contains("muse voice") {
            return .resetTurn
        }

        return .none
    }

    public static func isSpeechPerceptionError(_ event: LiveVoiceEvent) -> Bool {
        action(for: event) != .none
            || ((event.type == "error") && (event.code ?? "").lowercased().hasPrefix("asr_"))
    }
}
