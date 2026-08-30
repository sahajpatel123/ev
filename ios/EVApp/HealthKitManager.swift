import EVClient
import Foundation
import HealthKit

enum HealthKitError: Error {
    case unavailable
}

/// Read-only HealthKit bridge for Amazfit Helio (via Zepp → Apple Health)
/// and any other Health source. No write-back.
@MainActor
final class HealthKitManager {
    static let shared = HealthKitManager()

    private let store = HKHealthStore()

    private init() {}

    private var quantityIdentifiers: [HKQuantityTypeIdentifier] {
        var ids: [HKQuantityTypeIdentifier] = [
            .heartRate,
            .restingHeartRate,
            .heartRateVariabilitySDNN,
            .stepCount,
            .activeEnergyBurned,
            .oxygenSaturation,
            .respiratoryRate,
            .vo2Max,
        ]
        if #available(iOS 16.0, *) {
            ids.append(.appleSleepingWristTemperature)
        }
        return ids
    }

    func requestReadAccess() async throws {
        guard HKHealthStore.isHealthDataAvailable() else {
            throw HealthKitError.unavailable
        }
        var types = Set<HKObjectType>()
        for identifier in quantityIdentifiers {
            if let type = HKQuantityType.quantityType(forIdentifier: identifier) {
                types.insert(type)
            }
        }
        if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) {
            types.insert(sleep)
        }
        types.insert(HKObjectType.workoutType())
        try await store.requestAuthorization(toShare: [], read: types)
    }

    func enableBackgroundDelivery() async {
        for identifier in quantityIdentifiers {
            guard let type = HKQuantityType.quantityType(forIdentifier: identifier) else { continue }
            try? await store.enableBackgroundDelivery(for: type, frequency: .hourly)
        }
    }

    func latestMetrics() async -> (metrics: [String: Double], source: String) {
        var metrics: [String: Double] = [:]
        var sourceName = "healthkit"
        let now = Date()
        let dayAgo = now.addingTimeInterval(-86_400)

        func latest(_ identifier: HKQuantityTypeIdentifier, unit: HKUnit) async -> (Double, String)? {
            guard let type = HKQuantityType.quantityType(forIdentifier: identifier) else { return nil }
            let sort = NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)
            return await withCheckedContinuation { continuation in
                let query = HKSampleQuery(
                    sampleType: type,
                    predicate: HKQuery.predicateForSamples(withStart: dayAgo, end: now),
                    limit: 1,
                    sortDescriptors: [sort]
                ) { _, samples, _ in
                    guard let sample = samples?.first as? HKQuantitySample else {
                        continuation.resume(returning: nil)
                        return
                    }
                    continuation.resume(returning: (
                        sample.quantity.doubleValue(for: unit),
                        sample.sourceRevision.source.bundleIdentifier
                    ))
                }
                self.store.execute(query)
            }
        }

        if let (value, src) = await latest(.heartRate, unit: HKUnit.count().unitDivided(by: .minute())) {
            metrics["heart_rate"] = value
            sourceName = Self.mapSource(src, fallback: sourceName)
        }
        if let (value, src) = await latest(.restingHeartRate, unit: HKUnit.count().unitDivided(by: .minute())) {
            metrics["resting_hr"] = value
            sourceName = Self.mapSource(src, fallback: sourceName)
        }
        if let (value, src) = await latest(
            .heartRateVariabilitySDNN,
            unit: HKUnit.secondUnit(with: .milli)
        ) {
            metrics["hrv_ms"] = value
            sourceName = Self.mapSource(src, fallback: sourceName)
        }
        if let (value, _) = await latest(.stepCount, unit: .count()) {
            metrics["steps"] = value
        }
        if let (value, _) = await latest(.activeEnergyBurned, unit: .kilocalorie()) {
            metrics["active_kcal"] = value
        }
        if let (value, _) = await latest(.oxygenSaturation, unit: .percent()) {
            metrics["spo2"] = value * 100
        }
        if let (value, _) = await latest(
            .respiratoryRate,
            unit: HKUnit.count().unitDivided(by: .minute())
        ) {
            metrics["resp_rate"] = value
        }
        if let (value, _) = await latest(
            .vo2Max,
            unit: HKUnit.gramUnit(with: .milli).unitDivided(
                by: HKUnit.kilogram().unitMultiplied(by: .minute())
            )
        ) {
            metrics["vo2_max"] = value
        }
        if let sleepHours = await sleepHours(start: dayAgo, end: now) {
            metrics["sleep_hours"] = sleepHours
        }
        if let workout = await workoutMetrics(start: dayAgo, end: now) {
            metrics["workout_minutes"] = workout.minutes
            metrics["workout_count"] = Double(workout.count)
        }
        return (metrics, sourceName)
    }

    func publish(using client: EVAPIClient, deviceId: String) async {
        let snapshot = await latestMetrics()
        guard !snapshot.metrics.isEmpty else { return }
        let formatter = ISO8601DateFormatter()
        let syncedAt = formatter.string(from: Date())
        let units: [String: String] = [
            "heart_rate": "bpm",
            "resting_hr": "bpm",
            "hrv_ms": "ms",
            "steps": "count",
            "active_kcal": "kcal",
            "sleep_hours": "hours",
            "spo2": "percent",
            "resp_rate": "breaths/min",
            "vo2_max": "mL/kg/min",
            "workout_minutes": "minutes",
            "workout_count": "count",
        ]
        let sourceMetadata = [
            "healthkit_source": snapshot.source,
            "provider_chain": "Amazfit Helio -> Zepp -> Apple Health -> HealthKit -> EV iOS bridge",
        ]
        try? await client.postHealthSnapshot(
            source: snapshot.source,
            deviceId: deviceId,
            metrics: snapshot.metrics,
            syncedAt: syncedAt,
            units: units,
            sourceMetadata: sourceMetadata
        )
    }

    private func workoutMetrics(start: Date, end: Date) async -> (minutes: Double, count: Int)? {
        let type = HKObjectType.workoutType()
        return await withCheckedContinuation { continuation in
            let query = HKSampleQuery(
                sampleType: type,
                predicate: HKQuery.predicateForSamples(withStart: start, end: end),
                limit: HKObjectQueryNoLimit,
                sortDescriptors: nil
            ) { _, samples, _ in
                let workouts = samples as? [HKWorkout] ?? []
                guard !workouts.isEmpty else {
                    continuation.resume(returning: nil)
                    return
                }
                let minutes = workouts.reduce(0.0) { $0 + $1.duration } / 60.0
                continuation.resume(returning: (minutes: minutes, count: workouts.count))
            }
            self.store.execute(query)
        }
    }

    private func sleepHours(start: Date, end: Date) async -> Double? {
        guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { return nil }
        return await withCheckedContinuation { continuation in
            let query = HKSampleQuery(
                sampleType: type,
                predicate: HKQuery.predicateForSamples(withStart: start, end: end),
                limit: HKObjectQueryNoLimit,
                sortDescriptors: nil
            ) { _, samples, _ in
                let seconds = (samples as? [HKCategorySample] ?? []).reduce(0.0) { sum, sample in
                    let asleep: Set<Int>
                    if #available(iOS 16.0, *) {
                        asleep = [
                            HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,
                            HKCategoryValueSleepAnalysis.asleepCore.rawValue,
                            HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
                            HKCategoryValueSleepAnalysis.asleepREM.rawValue,
                        ]
                    } else {
                        asleep = [HKCategoryValueSleepAnalysis.asleep.rawValue]
                    }
                    guard asleep.contains(sample.value) else { return sum }
                    return sum + sample.endDate.timeIntervalSince(sample.startDate)
                }
                continuation.resume(returning: seconds > 0 ? seconds / 3600.0 : nil)
            }
            self.store.execute(query)
        }
    }

    private static func mapSource(_ bundle: String, fallback: String) -> String {
        let lowered = bundle.lowercased()
        if lowered.contains("zepp") || lowered.contains("amazfit") || lowered.contains("huami") {
            return "amazfit_helio"
        }
        return fallback
    }
}
