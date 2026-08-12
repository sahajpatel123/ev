import EVClient
import EVUI
import SwiftUI

@main
struct EVApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            AppShellView(client: appState.client, queue: appState.queue)
        }
    }
}
