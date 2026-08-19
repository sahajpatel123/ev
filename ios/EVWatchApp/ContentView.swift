import EVClient
import SwiftUI

/// Watch app: quick capture + HUD card. The complication extension renders
/// the same HUD payload through ``WatchComplicationStub``.
struct ContentView: View {
    @State private var card: HUDCard?
    @State private var note = ""
    @State private var status = ""
    @State private var confirming = false
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
                    if card.isApprovalHold {
                        Button(confirming ? "Confirming…" : "Confirm") {
                            confirmHold()
                        }
                        .disabled(confirming)
                        .font(.caption2)
                    }
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

    private func confirmHold() {
        guard !confirming, let card, card.isApprovalHold else { return }
        confirming = true
        Task {
            defer { confirming = false }
            let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
            do {
                if EVLifeBiometric.isAvailable {
                    let ok = await EVLifeBiometric.confirmLifeAction(
                        reason: "Confirm \(card.holdToolName ?? "this action")"
                    )
                    guard ok else { return }
                }
                if let actionId = card.holdActionId, !actionId.isEmpty {
                    _ = try await client.approveAction(id: actionId)
                } else if let name = card.holdToolName, !name.isEmpty {
                    _ = try await client.dispatchTool(
                        name: name,
                        arguments: card.holdArguments,
                        confirm: true
                    )
                }
                await load()
                status = "Confirmed"
            } catch {
                status = "Failed: \(error)"
            }
        }
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
