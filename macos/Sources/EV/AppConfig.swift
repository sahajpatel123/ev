import Foundation

/// Runtime configuration for the menu-bar app.
///
/// Resolution order: environment variable → UserDefaults → dev default.
/// The default points at a locally running EV backend so the app is useful
/// the moment `make dev` is up.
struct AppConfig {
    let baseURL: URL
    let apiKey: String
    let deviceID: String

    init() {
        let environment = ProcessInfo.processInfo.environment
        let defaults = UserDefaults.standard

        let urlString =
            environment["EV_API_URL"]
            ?? defaults.string(forKey: "EV_API_URL")
            ?? "http://127.0.0.1:8000"
        baseURL = URL(string: urlString) ?? URL(string: "http://127.0.0.1:8000")!

        apiKey =
            environment["EV_API_KEY"]
            ?? defaults.string(forKey: "EV_API_KEY")
            ?? "dev"

        let hostName = (Host.current().localizedName ?? "Mac")
            .lowercased()
            .replacingOccurrences(of: " ", with: "-")
        deviceID =
            environment["EV_DEVICE_ID"]
            ?? defaults.string(forKey: "EV_DEVICE_ID")
            ?? "mac-\(hostName)"
    }
}
