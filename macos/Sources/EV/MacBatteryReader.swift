import Foundation
import IOKit.ps

/// Real battery evidence for the Mac's sensor.read answers.
/// IOKit power source info — no shelling out, no guessing.
///
/// The IOPSKeys.h constants are CFString literals that Swift
/// does not re-export into IOKit.ps scope, so the raw key
/// strings (stable since 10.6, part of the IOKit API
/// contract) are used directly.
enum MacBatteryReader {
    private static let powerSourcesKey = "PS Power Sources"
    private static let typeKey = "Type"
    private static let internalBatteryType = "InternalBattery"
    private static let currentCapacityKey = "Current Capacity"
    private static let maxCapacityKey = "Max Capacity"
    private static let isChargingKey = "Is Charging"

    static func batteryPercent() -> Double? {
        guard let battery = firstBattery() else { return nil }
        let current = (battery[currentCapacityKey] as? NSNumber)?.doubleValue ?? 0
        let max = (battery[maxCapacityKey] as? NSNumber)?.doubleValue ?? 0
        guard max > 0 else { return nil }
        return (current / max) * 100.0
    }

    static func isCharging() -> Bool? {
        guard let battery = firstBattery() else { return nil }
        return battery[isChargingKey] as? Bool
    }

    private static func firstBattery() -> [String: Any]? {
        guard let blob = IOPSCopyPowerSourcesInfo()?.takeRetainedValue() as? [String: Any],
              let sources = blob[powerSourcesKey] as? [[String: Any]]
        else { return nil }
        return sources.first { source in
            (source[typeKey] as? String) == internalBatteryType
        }
    }
}
