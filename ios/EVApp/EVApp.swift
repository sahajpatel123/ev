import EVClient
import EVUI
import SwiftUI

@main
struct EVApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var appState = AppState()
    @Environment(\.scenePhase) private var scenePhase
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
                    Button {
                        showMesh = true
                    } label: {
                        Label("Evie Mesh", systemImage: "antenna.radiowaves.left.and.right")
                    }
                }
                .sheet(isPresented: $showGrantAccess) {
                    GrantAccessView()
                }
                .sheet(isPresented: $showMesh) {
                    MeshSettingsView(client: appState.client, deviceId: appState.registryDeviceId)
                }
                .onChange(of: appState.live.conversationId) { _, id in
                    appState.noteConversation(id)
                }
                .onChange(of: scenePhase) { _, phase in
                    guard phase == .active else { return }
                    Task { await appState.drainSyncIfEnabled() }
                }
                .task {
                     await appState.bootstrapIfNeeded()
                     await appState.drainSyncIfEnabled()
                     appState.startLiveIfPossible()
                     await appState.startHealthBridge()
                 }
        }
    }
}
