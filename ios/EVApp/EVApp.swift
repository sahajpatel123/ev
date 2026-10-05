import EVClient
import EVUI
import SwiftUI

@main
struct EVApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var appState = AppState()
    @State private var showGrantAccess = false
    @State private var showMesh = false

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
                .task {
                    await appState.bootstrapIfNeeded()
                    appState.startLiveIfPossible()
                    await appState.startHealthBridge()
                    // The iPhone joins the mesh: advertise, scan,
                    // report proximity, and execute intents.
                    MeshIntentRunner.shared.start(
                        client: appState.client,
                        deviceId: appState.registryDeviceId
                    )
                    EvieMeshStore.shared.start(
                        client: appState.client,
                        deviceId: appState.registryDeviceId,
                        capabilities: ["mesh", "camera", "text", "sensor"]
                    )
                }
        }
    }
}
