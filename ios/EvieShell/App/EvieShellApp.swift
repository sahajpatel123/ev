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

// Cycle 47 — iPhone-only, backward compat: additive theme-token struct (light/dark hex).
// Pure value type; EvieShellApp scene unchanged.
struct EvieThemeTokens: Sendable, Equatable {
    var backgroundLightHex: String = "#FFFFFF"
    var backgroundDarkHex: String = "#000000"
    var foregroundLightHex: String = "#111111"
    var foregroundDarkHex: String = "#F5F5F7"
    var accentLightHex: String = "#0A84FF"
    var accentDarkHex: String = "#0A84FF"
}
