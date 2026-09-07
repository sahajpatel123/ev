// Cycle EAC-70 — iPhone power and thermal budget.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// This policy consumes explicit telemetry supplied by the native client.  It
/// never guesses a battery level or thermal state, and it only describes what
/// a surface may do; it does not grant permission or start work itself.
import Foundation

public struct EviePowerBudget: Equatable, Sendable {
    public enum Phone: String, Sendable, CaseIterable {
        case iPhone16Pro
        case iPhoneSE
    }

    public enum ThermalState: String, Sendable, CaseIterable {
        case nominal
        case fair
        case serious
        case critical
        case unknown
    }

    public enum Workload: String, Sendable, CaseIterable {
        case text
        case capture
        case camera
        case liveVoice
        case backgroundSync
        case healthSnapshot
    }

    public enum Decision: String, Sendable {
        case allow
        case deferred
        case deny
    }

    public let phone: Phone
    public let batteryPercent: Int?
    public let lowPowerMode: Bool
    public let thermalState: ThermalState

    public init(
        phone: Phone,
        batteryPercent: Int?,
        lowPowerMode: Bool = false,
        thermalState: ThermalState = .unknown
    ) {
        self.phone = phone
        if let batteryPercent, (0...100).contains(batteryPercent) {
            self.batteryPercent = batteryPercent
        } else {
            self.batteryPercent = nil
        }
        self.lowPowerMode = lowPowerMode
        self.thermalState = thermalState
    }

    public func decision(for workload: Workload) -> Decision {
        if thermalState == .critical {
            return workload == .text || workload == .capture ? .allow : .deny
        }
        if thermalState == .serious {
            return workload == .text || workload == .capture ? .allow : .deferred
        }
        if let batteryPercent, batteryPercent <= 10 {
            return workload == .text || workload == .capture ? .allow : .deferred
        }
        if lowPowerMode || (batteryPercent.map { $0 <= 20 } ?? false) {
            switch workload {
            case .text, .capture:
                return .allow
            case .camera, .liveVoice, .backgroundSync, .healthSnapshot:
                return .deferred
            }
        }
        if batteryPercent == nil && workload == .backgroundSync {
            return .deferred
        }
        return .allow
    }

    public var displayLine: String {
        let phoneName = phone == .iPhone16Pro ? "iPhone 16 Pro" : "iPhone SE"
        let battery = batteryPercent.map { "Battery \($0)%" } ?? "Battery status unavailable"
        let thermal = thermalState == .unknown ? "thermal status unavailable" : thermalState.rawValue
        return "\(phoneName) · \(battery) · \(thermal)"
    }
}
