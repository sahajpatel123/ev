import AVFoundation
import EVClientObjC
import Foundation

/// Swift wrappers around Objective-C `@try` helpers. NSException cannot
/// cross a Swift frame, so the catch lives in `EVClientObjC`.
public enum AVAudioSafe {
    public static func attachAndPrepare(_ engine: AVAudioEngine) throws -> AVAudioFormat {
        var format: AVAudioFormat?
        var error: NSError?
        if !EVClientAudioAttachAndPrepare(engine, &format, &error) {
            throw error ?? EVAPIError.transport("audio engine prepare failed")
        }
        guard let format else {
            throw EVAPIError.transport("microphone input format is unavailable")
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
        if !EVClientAudioInstallTap(node, bufferSize, format, block, &error) {
            throw error ?? EVAPIError.transport("install tap failed")
        }
    }

    public static func removeTap(on node: AVAudioInputNode) {
        var error: NSError?
        _ = EVClientAudioRemoveTap(node, &error)
    }

    public static func start(_ engine: AVAudioEngine) throws {
        var error: NSError?
        if !EVClientAudioStartEngine(engine, &error) {
            throw error ?? EVAPIError.transport("audio engine failed to start")
        }
    }

    public static func stop(_ engine: AVAudioEngine) {
        var error: NSError?
        _ = EVClientAudioStopEngine(engine, &error)
    }
}
