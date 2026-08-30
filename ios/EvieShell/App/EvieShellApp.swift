import SwiftUI

@main
struct EvieShellApp: App {
    var body: some Scene {
        WindowGroup {
            WebCoreContainer()
                .ignoresSafeArea()
        }
    }
}
