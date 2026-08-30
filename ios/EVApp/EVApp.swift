import EVClient
import EVUI
import SwiftUI

@main
struct EVApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var appState = AppState()
    @State private var showGrantAccess = false

    var body: some Scene {
        WindowGroup {
            AppShellView(
                client: appState.client,
                queue: appState.queue,
                deviceId: appState.registryDeviceId,
                live: appState.live
            )
                .toolbar {
                    Button {
                        showGrantAccess = true
                    } label: {
                        Label("Grant access", systemImage: "hand.raised")
                    }
                }
                .sheet(isPresented: $showGrantAccess) {
                    GrantAccessView()
                }
                .onChange(of: appState.live.conversationId) { _, id in
                    appState.noteConversation(id)
                }
                .task {
                    await appState.bootstrapIfNeeded()
                    appState.startLiveIfPossible()
                    await appState.startHealthBridge()
                }
        }
    }
}
