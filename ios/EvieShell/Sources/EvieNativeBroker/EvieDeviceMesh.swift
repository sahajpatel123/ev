import Foundation

/// iPhone-only: device-mesh presence + dynamic executor scoring.
///
/// Each iPhone builds a lightweight presence heartbeat (battery, foreground,
/// permissions, camera rank, reachability) that the server mesh ledger can
/// compare across nodes. `bestExecutor` answers "which node should run this
/// capability" dynamically — no static primary/secondary, no hardcoded phone.
/// Pure Foundation — compile-checked on macOS.
public struct EvieDevicePresence: Equatable, Sendable {
    public var deviceID: String
    public var model: String
    public var cameraRank: Int
    public var batteryPercent: Int?
    public var lowPowerMode: Bool
    public var foreground: Bool
    public var reachableViaTailscale: Bool
    public var grantedPermissions: Set<String>
    public var lastSeenSecondsAgo: Int

    public init(
        deviceID: String,
        model: String = "iPhone",
        cameraRank: Int = 20,
        batteryPercent: Int? = nil,
        lowPowerMode: Bool = false,
        foreground: Bool = true,
        reachableViaTailscale: Bool = true,
        grantedPermissions: Set<String> = [],
        lastSeenSecondsAgo: Int = 0
    ) {
        self.deviceID = deviceID
        self.model = model
        self.cameraRank = cameraRank
        self.batteryPercent = batteryPercent
        self.lowPowerMode = lowPowerMode
        self.foreground = foreground
        self.reachableViaTailscale = reachableViaTailscale
        self.grantedPermissions = grantedPermissions
        self.lastSeenSecondsAgo = lastSeenSecondsAgo
    }

    public var heartbeat: [String: Any] {
        var payload: [String: Any] = [
            "device_id": deviceID,
            "model": model,
            "camera_preference_rank": cameraRank,
            "low_power_mode": lowPowerMode,
            "foreground": foreground,
            "reachable_via_tailscale": reachableViaTailscale,
            "granted_permissions": Array(grantedPermissions).sorted(),
            "last_seen_seconds_ago": lastSeenSecondsAgo,
        ]
        if let batteryPercent { payload["battery_percent"] = batteryPercent }
        return payload
    }
}

public enum EvieMeshRouter {
    /// Dynamic score: higher wins. Unreachable nodes never win; stale nodes
    /// (>120s) lose heavily; missing permission disqualifies gated actions.
    public static func score(_ node: EvieDevicePresence, capability: String, nowStaleThreshold: Int = 120) -> Int? {
        guard node.reachableViaTailscale else { return nil }
        if let required = EvieActionPlanner.permission(for: capability),
           !node.grantedPermissions.contains(required) { return nil }
        var score = 100
        if node.foreground { score += 20 }
        if node.lowPowerMode { score -= 15 }
        if let battery = node.batteryPercent, battery < 20 { score -= 20 }
        score -= min(node.cameraRank, 50)
        if node.lastSeenSecondsAgo > nowStaleThreshold { score -= 60 }
        return score
    }

    /// Pick the best node for a capability, or nil when none can run it.
    public static func bestExecutor(for capability: String, among nodes: [EvieDevicePresence]) -> EvieDevicePresence? {
        nodes.compactMap { node in score(node, capability: capability).map { (node, $0) } }
            .max { $0.1 < $1.1 }
            .map { $0.0 }
    }
}
