import EVClient
import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        application.registerForRemoteNotifications()
        registerPushWakeIfAvailable()
        return true
    }

    /// Push-wake wiring (EviePushWake): registers the BG refresh task once.
    /// No-op when EV_SYNC_DRAIN_ENABLED is off; safe default preserves behavior.
    private func registerPushWakeIfAvailable() {
#if canImport(BackgroundTasks)
        guard UserDefaults.standard.bool(forKey: "EV_SYNC_DRAIN_ENABLED") else { return }
        EviePushWake.registerBackgroundTask(
            makeClient: { Self.makeClient() },
            makeOutbox: { EvieEventOutbox.default() },
            makeCaptures: { Self.makeCaptures() }
        )
#endif
    }

    func application(
        _ application: UIApplication,
        didReceiveRemoteNotification userInfo: [AnyHashable: Any],
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        guard UserDefaults.standard.bool(forKey: "EV_SYNC_DRAIN_ENABLED") else {
            completionHandler(.noData)
            return
        }
#if canImport(BackgroundTasks)
        EviePushWake.handleSilentPush(
            userInfo: userInfo,
            makeClient: { Self.makeClient() },
            makeOutbox: { EvieEventOutbox.default() },
            makeCaptures: { Self.makeCaptures() },
            completionHandler: completionHandler
        )
#else
        completionHandler(.noData)
#endif
    }

    private static func makeClient() -> EVAPIClient {
        let config = EVClientAppConfig()
        let token = (try? KeychainTokenStore().load()) ?? "dev"
        return EVAPIClient(baseURL: config.resolvedBaseURL(), token: token)
    }

    private static func makeCaptures() -> OfflineCaptureQueue {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first?
            .appendingPathComponent("EVQueue", isDirectory: true)
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("EVQueue", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return OfflineCaptureQueue(store: FileCaptureQueueStore(directory: dir))
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in
            NotificationManager.shared.upload(deviceToken: deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // APNs needs a paid Apple Developer team; EV stays fully functional
        // without push — the failure is deliberately non-fatal.
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }
}
