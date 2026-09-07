// Cycle EAC-40 — iPhone device-tier capability flags.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// The two phones differ physically (iPhone 16 Pro vs iPhone SE): Dynamic
/// Island, Always-On display, Action button, and camera hardware are not
/// shared. This maps a named tier to the capability flags the Xcode-only UI
/// reads, so one codebase degrades gracefully on SE and lights up fully on
/// 16 Pro. Strings and bools only — Foundation alone, CLT compiles.
/// No backend change.

import Foundation

public struct EvieDeviceTier: Equatable, Sendable {
    public enum Tier: String, Sendable, Equatable, CaseIterable {
        case se
        case pro
    }

    public var tier: Tier

    public init(tier: Tier = .pro) {
        self.tier = tier
    }

    /// Heuristic tier from the device model name (e.g. "iPhone17,2" → pro,
    /// "iPhone14,6" → SE). Unknown models assume pro and degrade by runtime
    /// checks in the presenter, never by hiding features outright.
    public static func tier(forModelName name: String) -> Tier {
        let lower = name.lowercased()
        if lower.contains("se") { return .se }
        return .pro
    }

    public var supportsIsland: Bool { tier == .pro }
    public var supportsAlwaysOn: Bool { tier == .pro }
    public var supportsActionButton: Bool { tier == .pro }

    /// Both phones share the Taptic Engine vocabulary and lock-screen Live
    /// Activities, so haptics and lock-screen cues stay on everywhere.
    public var supportsHaptics: Bool { true }

    /// Display name for diagnostics. Example: "iPhone 16 Pro tier".
    public func displayName() -> String {
        switch tier {
        case .se:
            return "iPhone SE tier"
        case .pro:
            return "iPhone 16 Pro tier"
        }
    }
}
