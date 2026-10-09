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

    // Same UserDefaults key AppOrigin reads. Empty until the owner (or the
    // release IPA's Info.plist) provides the Home Station origin.
    @AppStorage("evie.api_origin") private var storedOrigin: String = ""

    var body: some Scene {
        WindowGroup {
            if AppOrigin.needsSetup(storedOrigin) {
                HomeStationSetupView { saved in
                    storedOrigin = saved
                }
            } else {
                WebCoreContainer()
                    .ignoresSafeArea()
                    .id(AppOrigin.apiOrigin)
            }
        }
    }
}

/// First-run Home Station origin entry.
///
/// Why this exists: a direct Xcode install bundles the loopback default and
/// the phone cannot reach the Mac at 127.0.0.1; the old workaround (a
/// `defaults write` from a Mac) is not available on iOS. Without this screen
/// the native shell white-screens and the owner falls back to the Safari
/// home-screen web app, where iOS blocks microphone capture.
struct HomeStationSetupView: View {
    var onSave: (String) -> Void

    @State private var value: String = ""
    @State private var error: String?

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "house.and.flag.fill")
                .font(.system(size: 44))
                .foregroundStyle(.tint)
            Text("Connect to your Home Station")
                .font(.title2.weight(.semibold))
                .multilineTextAlignment(.center)
            Text("Evie runs on your Mac. Enter its Tailscale HTTPS address. "
                + "It looks like https://your-mac.your-tailnet.ts.net")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            TextField("https://your-mac.your-tailnet.ts.net", text: $value)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled(true)
                .keyboardType(.URL)
                .textFieldStyle(.roundedBorder)
            if let error {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
            Button("Save & Connect") { save() }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            Text("Voice uses this app's microphone. iOS blocks the mic in "
                + "Safari home-screen web apps, so use this app for Talk.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(28)
    }

    private func save() {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            error = "Enter the Home Station address."
            return
        }
        let normalized = AppOrigin.normalized(trimmed)
        guard
            let url = URL(string: normalized),
            let scheme = url.scheme?.lowercased(),
            let host = url.host?.lowercased(),
            !host.isEmpty
        else {
            error = "Enter a full URL including https://"
            return
        }
        let loopback = host == "127.0.0.1" || host == "localhost" || host == "::1"
        guard scheme == "https" || loopback else {
            error = "Use the HTTPS Tailscale address (https://…ts.net)."
            return
        }
        error = nil
        onSave(normalized)
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
