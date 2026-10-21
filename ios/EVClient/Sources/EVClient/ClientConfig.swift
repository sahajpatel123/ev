/// Cross-platform client configuration shared by the iOS app, its share
/// extension, the watch app, and the macOS menu-bar app.

import Foundation
#if canImport(UIKit)
import UIKit
#endif

public struct EVClientAppConfig {
    public let baseURL: URL
    public let apiKey: String
    public let deviceID: String
    /// Opt-in flags. All default false = current behavior (direct route, legacy auth, no auto-drain).
    public let relayEnabled: Bool
    public let gatewayAuthEnabled: Bool
    public let syncDrainEnabled: Bool

    public init(defaultBaseURLString: String = "http://127.0.0.1:8000") {
        let info = Bundle.main.infoDictionary
        let urlString = info?["EV_API_URL"] as? String ?? defaultBaseURLString
        baseURL = URL(string: urlString) ?? URL(string: defaultBaseURLString)!
        apiKey = (try? KeychainTokenStore().load()) ?? "dev"

        let defaults = UserDefaults.standard
        relayEnabled = (info?["EV_RELAY_ENABLED"] as? Bool) ?? defaults.bool(forKey: "EV_RELAY_ENABLED")
        gatewayAuthEnabled = (info?["EV_GATEWAY_AUTH_ENABLED"] as? Bool) ?? defaults.bool(forKey: "EV_GATEWAY_AUTH_ENABLED")
        syncDrainEnabled = (info?["EV_SYNC_DRAIN_ENABLED"] as? Bool) ?? defaults.bool(forKey: "EV_SYNC_DRAIN_ENABLED")

        #if canImport(UIKit)
        deviceID = UIDevice.current.identifierForVendor?.uuidString ?? "ios-device"
        #else
        let host = (Host.current().localizedName ?? "device")
            .lowercased()
            .replacingOccurrences(of: " ", with: "-")
        deviceID = "mac-\(host)"
        #endif
    }

    /// Route selection: direct by default; relay base URL only when opted in.
    public func resolvedBaseURL(relayCandidate: EvieRelayCandidate? = nil) -> URL {
        guard relayEnabled else { return baseURL }
        let config = EvieRelayConfig(enabled: true, directBaseURL: baseURL)
        let sample = EvieRouteSample(candidate: relayCandidate, currentRoute: .direct)
        let decision = EvieRelayRoute.decide(config: config, sample: sample, trustedFingerprint: nil)
        return EvieRelayRoute.baseURL(config: config, decision: decision)
    }
}
