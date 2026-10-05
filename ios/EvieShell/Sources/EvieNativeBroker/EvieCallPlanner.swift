import Foundation

/// iPhone-only: dynamic call-path planner.
///
/// Picks how a "call X" request should be placed — native CallKit transaction
/// (system phone UI, recents, Bluetooth), FaceTime audio, or plain `tel:`
/// fallback — from contact data, requested kind, and runtime support, instead
/// of hardcoding one path. Pure Foundation so the package build verifies it.
public enum EvieCallPath: String, Equatable, Sendable {
    case callKit
    case faceTimeAudio
    case telFallback
}

public struct EvieCallPlan: Equatable, Sendable {
    public var path: EvieCallPath
    public var digits: String
    public var displayName: String
    public var reason: String

    public init(path: EvieCallPath, digits: String, displayName: String, reason: String) {
        self.path = path
        self.digits = digits
        self.displayName = displayName
        self.reason = reason
    }
}

public enum EvieCallPlanner {
    public static func plan(
        kind: String,
        digits: String,
        displayName: String,
        callKitAvailable: Bool
    ) -> EvieCallPlan? {
        let clean = digits.filter { $0.isNumber || $0 == "+" }
        guard !clean.isEmpty else { return nil }
        if kind == "facetime" {
            return .init(path: .faceTimeAudio, digits: clean, displayName: displayName, reason: "facetime_requested")
        }
        if callKitAvailable {
            return .init(path: .callKit, digits: clean, displayName: displayName, reason: "native_call_ui")
        }
        return .init(path: .telFallback, digits: clean, displayName: displayName, reason: "callkit_unavailable")
    }
}
