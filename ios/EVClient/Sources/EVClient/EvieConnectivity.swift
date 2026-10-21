// Cycle EAC-5x — iPhone connectivity + unified retry driver.
/// iPhone-only additive helper; backward compatible (new file, no existing API touched).
///
/// Replaces hardcoded `reachableViaTailscale`-style booleans with a real
/// NWPathMonitor-driven reachability signal, and unifies the four pure
/// retry verdicts (EvieRetryPlan, EvieRetryDelay, sync-scheduler and
/// poll-planner style verdicts) behind one jittered-backoff driver.
/// Existing policies are untouched; this file is an adapter layer.
///
/// Head-of-line blocking fix: queue flush skips a poisoned head-of-line
/// record (persistently failing with a non-retryable outcome) instead of
/// `break`-ing the whole drain — the remainder still flushes.

import Foundation
import Network

// MARK: - Reachability

/// Live reachability snapshot. Value type so call sites stay testable.
public struct EvieReachability: Equatable, Sendable {
    public var isReachable: Bool
    public var isExpensive: Bool
    public var isConstrained: Bool
    public var usesWiFi: Bool
    public var usesCellular: Bool

    public init(
        isReachable: Bool,
        isExpensive: Bool = false,
        isConstrained: Bool = false,
        usesWiFi: Bool = false,
        usesCellular: Bool = false
    ) {
        self.isReachable = isReachable
        self.isExpensive = isExpensive
        self.isConstrained = isConstrained
        self.usesWiFi = usesWiFi
        self.usesCellular = usesCellular
    }

    public static let offline = EvieReachability(isReachable: false)
    public static let wifi = EvieReachability(isReachable: true, usesWiFi: true)

    init(_ path: NWPath) {
        self.isReachable = path.status == .satisfied
        self.isExpensive = path.isExpensive
        self.isConstrained = path.isConstrained
        self.usesWiFi = path.usesInterfaceType(.wifi)
        self.usesCellular = path.usesInterfaceType(.cellular)
    }
}

/// NWPathMonitor-driven reachability. Drop-in replacement for hardcoded
/// `reachableViaTailscale` flags: observe `current` or subscribe via `onChange`.
public final class EvieConnectivityMonitor: @unchecked Sendable {
    private let monitor: NWPathMonitor
    private let queue: DispatchQueue
    private let lock = NSLock()
    private var _current: EvieReachability = .offline
    private var handlers: [UUID: @Sendable (EvieReachability) -> Void] = [:]

    public init(queueLabel: String = "com.evie.connectivity") {
        self.queue = DispatchQueue(label: queueLabel, qos: .utility)
        self.monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            self?.update(EvieReachability(path))
        }
        monitor.start(queue: queue)
    }

    deinit { monitor.cancel() }

    public var current: EvieReachability {
        lock.lock(); defer { lock.unlock() }
        return _current
    }

    /// Subscribe; returns a token for `unsubscribe`. Handler fires immediately with current value.
    @discardableResult
    public func subscribe(_ handler: @escaping @Sendable (EvieReachability) -> Void) -> UUID {
        let id = UUID()
        lock.lock()
        handlers[id] = handler
        let snapshot = _current
        lock.unlock()
        handler(snapshot)
        return id
    }

    public func unsubscribe(_ id: UUID) {
        lock.lock(); defer { lock.unlock() }
        handlers.removeValue(forKey: id)
    }

    /// Test seam: inject a path-derived snapshot.
    public func inject(_ reachability: EvieReachability) { update(reachability) }

    private func update(_ value: EvieReachability) {
        lock.lock()
        _current = value
        let snapshot = Array(handlers.values)
        lock.unlock()
        for h in snapshot { h(value) }
    }
}

// MARK: - Unified retry driver

/// One verdict shape every legacy policy maps into.
public struct EvieRetryVerdict: Equatable, Sendable {
    public var shouldRetry: Bool
    public var delaySeconds: Double
    public var attempt: Int
    public var maxAttempts: Int
    public var reason: String?

    public init(shouldRetry: Bool, delaySeconds: Double, attempt: Int = 0, maxAttempts: Int = 5, reason: String? = nil) {
        self.shouldRetry = shouldRetry
        self.delaySeconds = delaySeconds
        self.attempt = attempt
        self.maxAttempts = maxAttempts
        self.reason = reason
    }

    public static let giveUp = EvieRetryVerdict(shouldRetry: false, delaySeconds: 0)
}

/// Single retry driver: capped exponential backoff with ±25% full jitter.
public struct EvieRetryDriver: Sendable {
    public var baseDelaySeconds: Double
    public var maxDelaySeconds: Double
    public var maxAttempts: Int

    public init(baseDelaySeconds: Double = 2, maxDelaySeconds: Double = 300, maxAttempts: Int = 5) {
        self.baseDelaySeconds = max(0.5, baseDelaySeconds)
        self.maxDelaySeconds = max(self.baseDelaySeconds, maxDelaySeconds)
        self.maxAttempts = max(1, maxAttempts)
    }

    /// Backoff for `attempt` (0-based): base * 2^attempt, capped, ±25% jitter.
    /// `jitterAnchor` in [0,1) makes tests deterministic; default random.
    public func delay(attempt: Int, jitterAnchor: Double? = nil) -> Double {
        let raw = min(baseDelaySeconds * pow(2.0, Double(max(0, attempt))), maxDelaySeconds)
        let r = jitterAnchor ?? Double.random(in: 0..<1)
        let jitter = 0.75 + 0.5 * (r.truncatingRemainder(dividingBy: 1))
        return min(raw * jitter, maxDelaySeconds)
    }

    /// Verdict for an HTTP outcome. `nil` status = transport failure (retryable).
    public func verdict(statusCode: Int?, attempt: Int, jitterAnchor: Double? = nil) -> EvieRetryVerdict {
        let retryable = EvieRetryPlan.shouldRetry(statusCode: statusCode)
        guard retryable, attempt < maxAttempts else {
            return EvieRetryVerdict(shouldRetry: false, delaySeconds: 0, attempt: attempt, maxAttempts: maxAttempts,
                                    reason: retryable ? "attempts exhausted" : "non-retryable status \(statusCode.map(String.init) ?? "transport")")
        }
        return EvieRetryVerdict(shouldRetry: true, delaySeconds: delay(attempt: attempt, jitterAnchor: jitterAnchor),
                                attempt: attempt, maxAttempts: maxAttempts)
    }
}

// MARK: - Legacy adapters (existing policies untouched)

public enum EvieRetryAdapters: Sendable {
    /// EvieRetryPlan → unified verdict. Preserves plan's attempts/cap, adds jitter.
    public static func verdict(for plan: EvieRetryPlan, statusCode: Int?, jitterAnchor: Double? = nil) -> EvieRetryVerdict {
        let driver = EvieRetryDriver(baseDelaySeconds: plan.baseDelaySeconds, maxDelaySeconds: plan.maxDelaySeconds, maxAttempts: plan.maxAttempts)
        return driver.verdict(statusCode: statusCode, attempt: plan.attempt, jitterAnchor: jitterAnchor)
    }

    /// EvieRetryDelay (fixed schedule 5/15/60/180) → unified verdict. Always
    /// retryable-by-schedule; caller gates on connectivity/status first.
    public static func verdict(attempt: Int, schedule: @Sendable (Int) -> Int = EvieRetryDelay.seconds) -> EvieRetryVerdict {
        EvieRetryVerdict(shouldRetry: true, delaySeconds: Double(schedule(attempt)), attempt: attempt, maxAttempts: Int.max)
    }

    /// Sync-scheduler style verdict: (intervalSeconds, shouldRetryNow) → verdict.
    public static func syncSchedulerVerdict(intervalSeconds: Double, shouldRetryNow: Bool, attempt: Int = 0) -> EvieRetryVerdict {
        EvieRetryVerdict(shouldRetry: shouldRetryNow, delaySeconds: shouldRetryNow ? intervalSeconds : 0, attempt: attempt, maxAttempts: Int.max,
                         reason: shouldRetryNow ? nil : "scheduler suppressed")
    }

    /// Poll-planner style verdict: (nextPollIn, shouldPoll) → verdict.
    public static func pollPlannerVerdict(nextPollInSeconds: Double, shouldPoll: Bool, attempt: Int = 0) -> EvieRetryVerdict {
        EvieRetryVerdict(shouldRetry: shouldPoll, delaySeconds: shouldPoll ? nextPollInSeconds : 0, attempt: attempt, maxAttempts: Int.max,
                         reason: shouldPoll ? nil : "poll planner suppressed")
    }

    /// Offline-retry-policy style verdict: retryable flag + fixed delay → verdict.
    public static func offlinePolicyVerdict(isRetryable: Bool, delaySeconds: Double, attempt: Int = 0, maxAttempts: Int = 5) -> EvieRetryVerdict {
        EvieRetryVerdict(shouldRetry: isRetryable && attempt < maxAttempts, delaySeconds: isRetryable ? delaySeconds : 0,
                         attempt: attempt, maxAttempts: maxAttempts)
    }
}

// MARK: - Head-of-line-block fix for queue flush

public extension OfflineCaptureQueue {
    /// Drain that skips a non-retryable head-of-line record instead of
    /// aborting the whole flush. Transport failures still pause the drain
    /// (order preserved); 4xx validation outcomes quarantine-and-continue.
    func syncSkippingPoisonedHead(using client: EVAPIClient) async -> SyncSummary {
        let records: [PendingCapture]
        do { records = try store.load() } catch {
            return SyncSummary(synced: 0, dropped: 0, quarantined: 0, remaining: 0, errors: ["queue load failed: \(error)"])
        }
        guard !records.isEmpty else {
            return SyncSummary(synced: 0, dropped: 0, quarantined: 0, remaining: 0)
        }
        // Reuse the canonical sync for attachments/missing-file paths, then
        // handle the event path per-record: quarantine-and-continue on
        // non-retryable statuses instead of stopping at the head.
        var synced = 0, dropped = 0, quarantined = 0
        var remaining: [PendingCapture] = []
        var errors: [String] = []
        for record in records {
            if record.kind == "attachment", let attachment = record.attachment {
                guard FileManager.default.fileExists(atPath: attachment.filePath) else {
                    quarantined += 1
                    try? store.quarantine(record, reason: "attachment file missing: \(attachment.filePath)")
                    continue // skip poisoned head, keep draining
                }
            }
            do {
                let (status, data) = try await client.postEvent(payload: record.payload, idempotencyKey: record.idempotencyKey)
                switch status {
                case 201: synced += 1
                case 409: dropped += 1
                case 400, 422:
                    quarantined += 1
                    try? store.quarantine(record, reason: String(data: data, encoding: .utf8) ?? "validation failed")
                    continue // poisoned head quarantined; drain continues
                default:
                    if EvieRetryPlan.shouldRetry(statusCode: status) {
                        remaining.append(record) // retryable: preserve order, pause
                        errors.append("\(record.idempotencyKey): HTTP \(status)")
                        break
                    } else {
                        quarantined += 1
                        try? store.quarantine(record, reason: "non-retryable HTTP \(status)")
                        continue
                    }
                }
            } catch {
                remaining.append(record) // transport failure: pause, order preserved
                errors.append("\(record.idempotencyKey): \(error)")
                break
            }
        }
        // Anything not yet visited stays queued behind `remaining` in order.
        let visited = synced + dropped + quarantined + remaining.count
        if visited < records.count {
            remaining.append(contentsOf: records[visited...])
        }
        try? store.save(remaining)
        return SyncSummary(synced: synced, dropped: dropped, quarantined: quarantined, remaining: remaining.count, errors: errors)
    }
}
