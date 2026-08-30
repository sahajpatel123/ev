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
