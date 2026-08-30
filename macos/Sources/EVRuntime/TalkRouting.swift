import Foundation

/// What the Push to talk / ⇧⌘E handler should do.
///
/// Live duplex and clip PTT share one hardware input. Starting a second
/// `AVAudioEngine` tap on that device aborts the process — the “Talk
/// closes the app” crash.
public enum TalkAction: Equatable, Sendable {
    case toggleLiveMute
    case startClipCapture
    case stopClipCapture
    case ignore
}

/// Shipped Talk-routing function. `AppModel.toggleTalk` is a thin wrapper.
public enum TalkRouting {
    public static func liveOwnsInput(
        isLiveActive: Bool,
        isLiveMuted: Bool,
        liveIsRunning: Bool
    ) -> Bool {
        isLiveActive || isLiveMuted || liveIsRunning
    }

    public static func action(
        liveOwnsInput: Bool,
        isRecording: Bool,
        sendingVoice: Bool
    ) -> TalkAction {
        if liveOwnsInput {
            return .toggleLiveMute
        }
        if isRecording {
            return .stopClipCapture
        }
        if sendingVoice {
            return .ignore
        }
        return .startClipCapture
    }
}
