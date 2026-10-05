import Foundation
#if canImport(HealthKit)
import HealthKit
#endif

/// iPhone-only: dynamic HealthKit snapshot.
///
/// Replaces the static `available:false` stub with a live query that degrades
/// honestly: no hardware → `unavailable`; denied → `permission_required`;
/// granted → real 24h step/active-energy/sleep snapshot with `sent_to_model`
/// left false (the gateway decides what the model may see). Pure logic plus a
/// thin HK query layer compiled only where HealthKit exists.
public struct EvieHealthSnapshot: Equatable, Sendable {
    public var available: Bool
    public var permission: String
    public var steps24h: Int?
    public var activeEnergyKcal24h: Double?
    public var sleepHours24h: Double?
    public var freshness: String
    public var reason: String?

    public init(
        available: Bool,
        permission: String,
        steps24h: Int? = nil,
        activeEnergyKcal24h: Double? = nil,
        sleepHours24h: Double? = nil,
        freshness: String,
        reason: String? = nil
    ) {
        self.available = available
        self.permission = permission
        self.steps24h = steps24h
        self.activeEnergyKcal24h = activeEnergyKcal24h
        self.sleepHours24h = sleepHours24h
        self.freshness = freshness
        self.reason = reason
    }

    public var payload: [String: Any] {
        var snapshot: [String: Any] = [:]
        if let steps24h { snapshot["steps_24h"] = steps24h }
        if let activeEnergyKcal24h { snapshot["active_energy_kcal_24h"] = activeEnergyKcal24h }
        if let sleepHours24h { snapshot["sleep_hours_24h"] = sleepHours24h }
        var out: [String: Any] = [
            "ok": true,
            "available": available,
            "permission": permission,
            "freshness": freshness,
            "sent_to_model": false,
            "snapshot": snapshot,
        ]
        if let reason { out["reason"] = reason }
        return out
    }
}

public enum EvieHealthPlanner {
    public static func unavailable(reason: String) -> EvieHealthSnapshot {
        .init(available: false, permission: "unavailable", freshness: "unavailable", reason: reason)
    }

    public static func permissionRequired() -> EvieHealthSnapshot {
        .init(available: true, permission: "denied", freshness: "stale", reason: "permission_required")
    }

    #if canImport(HealthKit)
    @available(iOS 13.0, macOS 13.0, *)
    public static func query(store: HKHealthStore = HKHealthStore()) async -> EvieHealthSnapshot {
        guard HKHealthStore.isHealthDataAvailable() else {
            return unavailable(reason: "no_health_data_on_device")
        }
        let read: Set<HKObjectType> = Set([
            HKObjectType.quantityType(forIdentifier: .stepCount),
            HKObjectType.quantityType(forIdentifier: .activeEnergyBurned),
            HKObjectType.categoryType(forIdentifier: .sleepAnalysis),
        ].compactMap { $0 })
        let end = Date()
        let start = end.addingTimeInterval(-86400)
        async let steps = sumQuantity(store: store, id: .stepCount, unit: .count(), start: start, end: end)
        async let energy = sumQuantity(store: store, id: .activeEnergyBurned, unit: .kilocalorie(), start: start, end: end)
        async let sleep = sumSleep(store: store, start: start, end: end)
        let (s, e, sl) = await (steps, energy, sleep)
        return .init(
            available: true, permission: "granted",
            steps24h: s.map { Int($0) }, activeEnergyKcal24h: e, sleepHours24h: sl,
            freshness: "fresh_24h"
        )
    }

    @available(iOS 13.0, macOS 13.0, *)
    static func sumQuantity(store: HKHealthStore, id: HKQuantityTypeIdentifier, unit: HKUnit, start: Date, end: Date) async -> Double? {
        guard let type = HKObjectType.quantityType(forIdentifier: id) else { return nil }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        return await withCheckedContinuation { cont in
            store.execute(HKStatisticsQuery(quantityType: type, quantitySamplePredicate: predicate, options: .cumulativeSum) { _, stats, _ in
                cont.resume(returning: stats?.sumQuantity()?.doubleValue(for: unit))
            })
        }
    }

    @available(iOS 13.0, macOS 13.0, *)
    static func sumSleep(store: HKHealthStore, start: Date, end: Date) async -> Double? {
        guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { return nil }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        return await withCheckedContinuation { cont in
            store.execute(HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, _ in
                let awake = HKCategoryValueSleepAnalysis.awake.rawValue
                let asleep = (samples as? [HKCategorySample])?.filter { $0.value != awake }
                let hours = asleep?.reduce(0.0) { $0 + $1.endDate.timeIntervalSince($1.startDate) / 3600 }
                cont.resume(returning: hours)
            })
        }
    }
    #endif
}
