import Foundation

/// iPhone-only: adaptive notification-poll cadence.
///
/// With no APNs environment the phones poll. A fixed interval either drains
/// battery or delivers late, so this planner answers the cadence dynamically
/// from live fitness: foreground phones poll fast, background/low-power or
/// unreachable phones back off or pause. Pure Foundation.
public enum EviePollPlanner {
    public static func intervalSeconds(
        foreground: Bool,
        lowPowerMode: Bool,
        reachableViaTailscale: Bool,
        hasUrgentItems: Bool = false
    ) -> Int? {
        guard reachableViaTailscale else { return nil }
        if foreground {
            if hasUrgentItems { return 10 }
            return lowPowerMode ? 60 : 15
        }
        return lowPowerMode ? 900 : 300
    }
}
