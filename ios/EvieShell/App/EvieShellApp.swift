import SwiftUI
import UserNotifications
#if canImport(UIKit)
import UIKit
#endif

#if os(iOS)
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            UserDefaults.standard.set(granted ? "granted" : "denied", forKey: "evie.notification_auth")
        }
        return true
    }
}
#endif

@main
struct EvieShellApp: App {
    #if os(iOS)
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    #endif

    var body: some Scene {
        WindowGroup {
            WebCoreContainer()
                .ignoresSafeArea()
        }
    }
}
