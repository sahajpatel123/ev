import Foundation
#if os(iOS)
import UIKit
#endif

public enum FeedbackEngine {
    public static func play(_ event: HapticEvent) {
        #if os(iOS)
        switch event {
        case .selection, .actionUnderstood, .visionCapture:
            UISelectionFeedbackGenerator().selectionChanged()
        case .confirmationRequested, .voiceStarted, .voiceStopped, .deviceSwitch:
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
        case .confirmationAccepted, .actionSuccess:
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        case .actionFailure:
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
        }
        #endif
    }
}

// Cycle 46 — iPhone-only, backward compat: orb-state haptic mapping helper.
// Additive extension; existing FeedbackEngine.play(_:) unchanged.
public extension FeedbackEngine {
    static func play(forOrbState state: EvieOrbState) {
        switch state {
        case .idle, .thinking: play(.selection)
        case .listening: play(.voiceStarted)
        case .speaking: play(.voiceStopped)
        case .confirming: play(.confirmationRequested)
        case .succeeded: play(.actionSuccess)
        case .failed: play(.actionFailure)
        }
    }
}
