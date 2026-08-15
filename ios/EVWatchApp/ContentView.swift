import EVClient
import SwiftUI

/// Watch app: quick capture + HUD card. The complication extension renders
/// the same HUD payload through ``WatchComplicationStub``.
struct ContentView: View {
    @State private var card: HUDCard?
    @State private var note = ""
    @State private var status = ""
    private let config = EVClientAppConfig()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                Text("EV")
                    .font(.headline)
                if let card {
                    Text(card.renderText())
                        .font(.caption2)
                        .multilineTextAlignment(.leading)
                } else {
                    Text("Loading…")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                TextField("Ask EV", text: $note)
                Button("Ask") {
                    ask()
                }
                Button("Remember") {
                    remember()
                }
                if !status.isEmpty {
                    Text(status)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding()
        }
        .task { await load() }
    }

    private func load() async {
        let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        card = try? await client.hudCard()
    }

    private func ask() {
        let text = note
        note = ""
        Task {
            let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
            do {
                let result = try await client.lookoutUtterance(text: text)
                status = result.reply
                if let hud = result.hud {
                    card = hud
                }
            } catch {
                status = "Failed: \(error)"
            }
        }
    }

    private func remember() {
        let text = note
        note = ""
        Task {
            let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
            do {
                let result = try await client.capture(
                    payload: CapturePayload(text: text, deviceID: config.deviceID)
                )
                status = result.duplicate ? "Already captured" : "Captured"
            } catch {
                status = "Failed: \(error)"
            }
        }
    }
}
