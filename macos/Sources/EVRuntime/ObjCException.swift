import EVRuntimeObjC
import AVFoundation
import Foundation
import UserNotifications

/// Swift wrappers around the Objective-C `@try` helpers. NSException cannot
/// cross a Swift frame, so the catch lives in `EVRuntimeObjC`.
public enum ObjCException {
    public static func attachAndPrepare(_ engine: AVAudioEngine) throws -> AVAudioFormat {
        var format: AVAudioFormat?
        var error: NSError?
        if !EVAudioAttachAndPrepare(engine, &format, &error) {
            throw error ?? Self.failed("audio engine prepare")
        }
        guard let format else {
            throw Self.failed("microphone input format is unavailable")
        }
        return format
    }

    public static func installTap(
        on node: AVAudioInputNode,
        bufferSize: AVAudioFrameCount,
        format: AVAudioFormat,
        block: @escaping AVAudioNodeTapBlock
    ) throws {
        var error: NSError?
        if !EVAudioInstallTap(node, bufferSize, format, block, &error) {
            throw error ?? Self.failed("install tap")
        }
    }

    public static func removeTap(on node: AVAudioInputNode) {
        var error: NSError?
        _ = EVAudioRemoveTap(node, &error)
    }

    public static func start(_ engine: AVAudioEngine) throws {
        var error: NSError?
        if !EVAudioStartEngine(engine, &error) {
            throw error ?? Self.failed("audio engine start")
        }
    }

    public static func stop(_ engine: AVAudioEngine) {
        var error: NSError?
        _ = EVAudioStopEngine(engine, &error)
    }

    public static func notificationCenter() -> UNUserNotificationCenter? {
        EVNotificationCenterOrNil()
    }

    /// Proves the ObjC `@try` is actually reached (NSException never crosses Swift).
    public static func raiseAndCatchForTests() throws {
        var error: NSError?
        if EVRaiseAndCatchForTests(&error) {
            throw Self.failed("expected test exception")
        }
        if let error {
            throw error
        }
    }

    private static func failed(_ message: String) -> NSError {
        NSError(
            domain: "EVException",
            code: 0,
            userInfo: [NSLocalizedDescriptionKey: message]
        )
    }
}
