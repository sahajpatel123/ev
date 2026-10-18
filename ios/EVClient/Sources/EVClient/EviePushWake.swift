import Foundation

#if canImport(BackgroundTasks)
import BackgroundTasks
#endif
#if canImport(UIKit)
import UIKit
#endif

/// Slice 4 — APNs silent-push wake + background drain.
///
/// WIRING (app target, `ios/EVApp` — not done here, notes only):
/// 1. `Info.plist`: add `UIBackgroundModes = ["remote-notification", "fetch"]`
///    and `BGTaskSchedulerPermittedIdentifiers = ["com.evie.sync.refresh"]`.
/// 2. `AppDelegate.didFinishLaunching`: call `EviePushWake.registerBackgroundTask(clientProvider:)`.
/// 3. `AppDelegate.application(_:didReceiveRemoteNotification:fetchCompletionHandler:)`:
///    call `await EviePushWake.handleSilentPush(userInfo:fetchCompletionHandler:)`.
/// 4. Device-token upload stays in `NotificationManager.upload(deviceToken:)`;
///    this file only reads the cached hex token (`ev.apnsToken`) for diagnostics.
///
/// Fallback when push is unavailable (simulator, no paid team, user denied):
/// `EviePushWake.scheduleRefresh fallback` is a no-op and sync still runs on
/// foreground launch + `BGAppRefreshTask` when the OS grants it. Push is an
/// optimization, never a correctness dependency.

public enum EviePushWake {
    public static let refreshTaskId = "com.evie.sync.refresh"
    private static let tokenDefaultsKey = "ev.apnsToken"

    public struct DrainResult: Sendable, Equatable {
        public let outboxFlushed: Int
        public let outboxRemaining: Int
        public let capturesSynced: Int
        public let pullsApplied: Int
        public let inboxFetched: Int
        public init(outboxFlushed: Int, outboxRemaining: Int, capturesSynced: Int, pullsApplied: Int, inboxFetched: Int = 0) {
            self.outboxFlushed = outboxFlushed
            self.outboxRemaining = outboxRemaining
            self.capturesSynced = capturesSynced
            self.pullsApplied = pullsApplied
            self.inboxFetched = inboxFetched
        }
    }

    /// True when a silent push (`content-available: 1`) should trigger a drain.
    public static func isSilentPush(_ userInfo: [AnyHashable: Any]) -> Bool {
        let aps = userInfo["aps"] as? [String: Any]
        return (aps?["content-available"] as? Int) == 1
    }

    /// Drain sequence: outbox flush → capture queue sync → pull-to-quiescence → inbox fetch.
    /// Order matters: local mutations go first so the pull sees server state
    /// that includes our writes; all steps are idempotent so repeats are safe.
    /// The inbox fetch runs last and only when `fetchInbox` is true; it is
    /// best-effort (failures yield 0) so a gateway outage never fails the drain.
    @discardableResult
    public static func drain(
        client: EVAPIClient,
        outbox: EvieEventOutbox,
        captures: OfflineCaptureQueue,
        pull: @Sendable ([EvieSyncEvent]) async throws -> Void,
        fetchInbox: Bool = false
    ) async -> DrainResult {
        let flushed = await outbox.flush(using: client)
        let summary = await captures.sync(using: client)
        // Best-effort in background: a failed pull must not fail the task.
        let coordinator = EvieEventSync(client: client)
        let applied = (try? await coordinator.pullToQuiescence(apply: pull)) ?? 0
        let inboxCount: Int
        if fetchInbox {
            inboxCount = (try? await client.fetchInboxPayload().items.count) ?? 0
        } else {
            inboxCount = 0
        }
        return DrainResult(
            outboxFlushed: flushed.flushed,
            outboxRemaining: flushed.remaining,
            capturesSynced: summary.synced,
            pullsApplied: applied,
            inboxFetched: inboxCount
        )
    }

    /// Cached APNs token hex for diagnostics; nil when push unavailable.
    public static func cachedDeviceToken(defaults: UserDefaults = .standard) -> String? {
        defaults.string(forKey: tokenDefaultsKey)
    }

    public static var isPushAvailable: Bool { cachedDeviceToken() != nil }

    // MARK: - OS wiring (UIKit/BackgroundTasks only; no-ops in tests)

#if canImport(UIKit) && canImport(BackgroundTasks)
    /// Call once from `didFinishLaunchingWithOptions`.
    public static func registerBackgroundTask(
        makeClient: @escaping @Sendable () -> EVAPIClient,
        makeOutbox: @escaping @Sendable () -> EvieEventOutbox,
        makeCaptures: @escaping @Sendable () -> OfflineCaptureQueue
    ) {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: refreshTaskId,
            using: nil
        ) { task in
            guard let refresh = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            // Earliest-begin-date reschedule keeps the drain periodic even
            // when pushes never arrive (the push-unavailable fallback).
            scheduleRefresh()
            let work = Task {
                _ = await drain(
                    client: makeClient(),
                    outbox: makeOutbox(),
                    captures: makeCaptures(),
                    pull: { _ in }
                )
                refresh.setTaskCompleted(success: true)
            }
            refresh.expirationHandler = {
                work.cancel()
                refresh.setTaskCompleted(success: false)
            }
        }
    }

    /// Best-effort schedule; silently skips when BackgroundTasks unavailable.
    public static func scheduleRefresh(earliest: TimeInterval = 15 * 60) {
        let request = BGAppRefreshTaskRequest(identifier: refreshTaskId)
        request.earliestBeginDate = Date(timeIntervalSinceNow: earliest)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Call from `didReceiveRemoteNotification:fetchCompletionHandler:`.
    /// Calls `completionHandler` exactly once with the right UIBackgroundFetchResult.
    public static func handleSilentPush(
        userInfo: [AnyHashable: Any],
        makeClient: @escaping @Sendable () -> EVAPIClient,
        makeOutbox: @escaping @Sendable () -> EvieEventOutbox,
        makeCaptures: @escaping @Sendable () -> OfflineCaptureQueue,
        completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        guard isSilentPush(userInfo) else {
            completionHandler(.noData)
            return
        }
        Task {
            let before = (try? makeCaptures().pending().count) ?? 0
            let result = await drain(
                client: makeClient(),
                outbox: makeOutbox(),
                captures: makeCaptures(),
                pull: { _ in }
            )
            // Opportunistically re-schedule so a push also refreshes the
            // periodic fallback cadence.
            scheduleRefresh()
            let newData: Bool = result.outboxFlushed > 0 || result.capturesSynced > 0
                || result.pullsApplied > 0 || result.inboxFetched > 0 || before > result.outboxRemaining
            completionHandler(newData ? .newData : .noData)
        }
    }
    #endif
}

extension EVAPIClient {
    /// Best-effort `GET /v1/device-gateway/inbox` poll-channel fetch for the push-wake drain.
    public func fetchInboxPayload() async throws -> EvieInboxPayload {
        let (_, data) = try await send("/v1/device-gateway/inbox")
        return try JSONDecoder().decode(EvieInboxPayload.self, from: data)
    }
}
