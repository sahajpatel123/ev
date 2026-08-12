import HealthKit

enum HealthKitError: Error {
    case unavailable
}

/// Read-only HealthKit access: heart rate, HRV, sleep, steps, and energy.
/// No write-back, matching the client architecture contract.
@MainActor
final class HealthKitManager {
    static let shared = HealthKitManager()

    private let store = HKHealthStore()

    private init() {}

    func requestReadAccess() async throws {
        guard HKHealthStore.isHealthDataAvailable() else {
            throw HealthKitError.unavailable
        }
        var types = Set<HKObjectType>()
        let quantityIdentifiers: [HKQuantityTypeIdentifier] = [
            .heartRate,
            .restingHeartRate,
            .heartRateVariabilitySDNN,
            .stepCount,
            .activeEnergyBurned,
        ]
        for identifier in quantityIdentifiers {
            if let type = HKQuantityType.quantityType(forIdentifier: identifier) {
                types.insert(type)
            }
        }
        if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) {
            types.insert(sleep)
        }
        try await store.requestAuthorization(toShare: [], read: types)
    }
}
