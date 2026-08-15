import AppKit
import EVClient
import EVUI
import SwiftUI

/// The menu-bar panel: status, always-available capture, chat, HUD card,
/// permissions, and launch-at-login.
struct MenuBarView: View {
    @EnvironmentObject private var model: AppModel
    @State private var chatDraft = ""
    @State private var showPermissions = false
    @AppStorage("ev.hasSeenLifeGrant") private var hasSeenLifeGrant = false

    var body: some View {
        Group {
            if showPermissions {
                PermissionsPanelView(onBack: { showPermissions = false })
            } else {
                homeContent
            }
        }
        .padding(12)
        .onAppear {
            model.start()
            if !hasSeenLifeGrant {
                showPermissions = true
                hasSeenLifeGrant = true
            }
        }
    }

    private var homeContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            statusHeader
            Divider()
            captureRow
            Divider()
            chatSection
            if let hud = model.hudCard {
                Divider()
                HUDCardView(card: hud)
                    .padding(.vertical, 2)
            }
            Divider()
            footerRow
        }
    }

    private var statusHeader: some View {
        HStack {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            Text("EVIE")
                .font(.headline)
            Text(model.status.label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            if model.isLiveMuted {
                Text("muted")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            } else if model.isLiveActive {
                Text("live")
                    .font(.caption2)
                    .foregroundStyle(.green)
            } else if model.isRecording {
                Text("REC · \(Int(MicCapture.maxSeconds))s max")
                    .font(.caption2)
                    .foregroundStyle(.red)
            }
            if model.queueCount > 0 {
                Text("\(model.queueCount) queued")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
    }

    private var statusColor: Color {
        switch model.status {
        case .offline: return .gray
        case .listening: return .green
        case .thinking: return .blue
        case .speaking: return .purple
        }
    }

    private var captureRow: some View {
        HStack(spacing: 6) {
            TextField("Capture…", text: $model.captureText)
                .textFieldStyle(.roundedBorder)
                .onSubmit {
                    model.capture()
                }
            Button("Remember") {
                model.capture()
            }
        }
    }

    private var chatSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Conversation")
                .font(.caption)
                .foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(model.messages) { message in
                        Text("\(message.role): \(message.text)")
                            .font(.body)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if model.messages.isEmpty {
                        Text("Just talk — EV is listening. No need to say EVIE.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .frame(maxHeight: 180)

            HStack(spacing: 6) {
                TextField("Ask EVIE…", text: $chatDraft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit {
                        let text = chatDraft
                        chatDraft = ""
                        model.sendChat(text)
                    }
                Button("Send") {
                    let text = chatDraft
                    chatDraft = ""
                    model.sendChat(text)
                }
            }

            HStack(spacing: 8) {
                Spacer()
                Button(liveButtonTitle) {
                    model.toggleTalk()
                }
                .font(.caption)
            }
            if let error = model.lastError {
                Text(error)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var footerRow: some View {
        HStack {
            Toggle("Open at login", isOn: launchAtLoginBinding)
                .font(.caption)
                .toggleStyle(.checkbox)
            Spacer()
            if PresenceController.shared.lastContent != nil {
                Button("Last lookouts") {
                    PresenceController.shared.showLast()
                }
                .font(.caption)
            }
            Button("Permissions") {
                showPermissions = true
            }
            .font(.caption)
            Button("Quit") {
                AppLifecycle.quit()
            }
            .font(.caption)
        }
    }

    private var liveButtonTitle: String {
        if model.isLiveActive || model.isLiveMuted {
            return model.isLiveMuted ? "Unmute" : "Mute"
        }
        return model.isRecording ? "Stop & send" : "Push to talk"
    }

    private var launchAtLoginBinding: Binding<Bool> {
        Binding(
            get: { model.launchAtLogin },
            set: { model.launchAtLogin = $0 }
        )
    }
}
