// Cycle EAC-15 — iPhone location-aware HUD filter.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Decides on-device whether a HUD card deserves surfacing based on distance,
/// so both iPhones stay quiet unless location makes the card relevant.
/// Takes already-computed meters (CoreLocation stays in the Xcode target);
/// Foundation only so CLT compiles. No backend change.

import Foundation

public struct EvieLocationHUD: Equatable, Sendable {
    public var distanceMeters: Double?
    public var radiusMeters: Double

    public init(distanceMeters: Double? = nil, radiusMeters: Double = 500) {
        self.distanceMeters = distanceMeters
        self.radiusMeters = max(1, radiusMeters)
    }

    /// True when the card should surface: no location attached (not
    /// location-gated) or inside the radius.
    public var shouldSurface: Bool {
        guard let distanceMeters else { return true }
        return distanceMeters <= radiusMeters
    }

    /// Compact line for the Today view. Examples: "nearby · 120m",
    /// "2.3km away", "no location".
    public func displayLine() -> String {
        guard let distanceMeters else { return "no location" }
        if distanceMeters < 0 { return "no location" }
        if distanceMeters < 1000 {
            return shouldSurface
                ? "nearby · \(Int(distanceMeters))m"
                : "\(Int(distanceMeters))m away"
        }
        let km = distanceMeters / 1000
        return String(format: "%.1fkm away", km)
    }
}
