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

    public init(defaultBaseURLString: String = "http://127.0.0.1:8000") {
        let info = Bundle.main.infoDictionary
        let urlString = info?["EV_API_URL"] as? String ?? defaultBaseURLString
        baseURL = URL(string: urlString) ?? URL(string: defaultBaseURLString)!
        apiKey = (try? KeychainTokenStore().load()) ?? "dev"

        #if canImport(UIKit)
        deviceID = UIDevice.current.identifierForVendor?.uuidString ?? "ios-device"
        #else
        let host = (Host.current().localizedName ?? "device")
            .lowercased()
            .replacingOccurrences(of: " ", with: "-")
        deviceID = "mac-\(host)"
        #endif
    }
}
