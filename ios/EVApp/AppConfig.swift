import EVClient

/// iOS app configuration wrapper over ``EVClientAppConfig``.
struct AppConfig {
    let baseURL: URL
    let apiKey: String
    let deviceID: String

    init() {
        let config = EVClientAppConfig()
        baseURL = config.baseURL
        apiKey = config.apiKey
        deviceID = config.deviceID
    }
}
