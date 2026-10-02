import Foundation

/// iPhone-only: dynamic vision-capture policy.
///
/// Answers "how rich a frame stream should this phone send" from live device
/// fitness — camera rank, battery, low-power mode, foreground — instead of a
/// fixed interval for every phone. 16 Pro streams denser than SE; a hot or
/// low-battery phone throttles itself. Pure Foundation.
public struct EvieVisionPolicy: Equatable, Sendable {
    public var frameIntervalSeconds: Double
    public var maxFrames: Int
    public var jpegQuality: Double
    public var reason: String

    public init(frameIntervalSeconds: Double, maxFrames: Int, jpegQuality: Double, reason: String) {
        self.frameIntervalSeconds = frameIntervalSeconds
        self.maxFrames = maxFrames
        self.jpegQuality = jpegQuality
        self.reason = reason
    }
}

public enum EvieVisionPlanner {
    public static func plan(
        cameraRank: Int,
        batteryPercent: Int?,
        lowPowerMode: Bool,
        foreground: Bool
    ) -> EvieVisionPolicy {
        guard foreground else {
            return .init(frameIntervalSeconds: 5.0, maxFrames: 2, jpegQuality: 0.5, reason: "background")
        }
        if lowPowerMode || (batteryPercent.map { $0 < 20 } ?? false) {
            return .init(frameIntervalSeconds: 3.0, maxFrames: 4, jpegQuality: 0.6, reason: "power_saver")
        }
        if cameraRank <= 0 {
            return .init(frameIntervalSeconds: 1.2, maxFrames: 12, jpegQuality: 0.85, reason: "pro_camera")
        }
        if cameraRank <= 10 {
            return .init(frameIntervalSeconds: 1.8, maxFrames: 8, jpegQuality: 0.75, reason: "standard_camera")
        }
        return .init(frameIntervalSeconds: 2.5, maxFrames: 6, jpegQuality: 0.7, reason: "fallback_camera")
    }
}
