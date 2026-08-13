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

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            statusHeader
            Divider()
            HandsFreeSectionView(session: model.handsFree)
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
        .padding(12)
        .sheet(isPresented: $showPermissions) {
            PermissionsPanelView()
        }
        .onAppear {
            model.start()
            model.hotkey.start(
                keyCode: 14, // "e"
                flags: [.command, .shift],
                handler: { [weak model] in
                    Task { @MainActor in
                        model?.toggleTalk()
                    }
                }
            )
        }
    }

    private var statusHeader: some View {
        HStack {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            Text("EV")
                .font(.headline)
            Text(model.status.label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            if model.isRecording {
                Text("REC")
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
                        Text("Ask EV anything — the reply streams here.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .frame(maxHeight: 180)

            HStack(spacing: 6) {
                TextField("Ask EV…", text: $chatDraft)
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
                Button(model.isRecording ? "Stop & send" : "Talk") {
                    model.toggleTalk()
                }
                Text("⇧⌘E")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Permissions…") {
                    showPermissions.toggle()
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
            Toggle("Launch at login", isOn: Binding(
                get: { model.launchAtLogin },
                set: { model.launchAtLogin = $0 }
            ))
            .toggleStyle(.checkbox)
            .font(.caption)
            Spacer()
            Button("Quit") {
                NSApplication.shared.terminate(nil)
            }
            .font(.caption)
        }
    }
}

/// Always-on listening: the switch, what the loop is doing right now, the live
/// caption, and the last exchange.
///
/// Its own view because ``HandsFreeSession`` is a second observable object —
/// observing ``AppModel`` alone would not redraw when the stream changes.
struct HandsFreeSectionView: View {
    @ObservedObject var session: HandsFreeSession

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle("Hands-free — say “EVIE”", isOn: $session.isEnabled)
                .toggleStyle(.switch)
                .font(.subheadline)

            HStack(spacing: 8) {
                Text(session.state.label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ProgressView(value: min(max(session.level, 0), 1))
                    .progressViewStyle(.linear)
                    .frame(width: 90)
                Spacer()
            }

            if !session.caption.isEmpty {
                Text(session.caption)
                    .font(.callout)
                    .italic()
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !session.lastTranscript.isEmpty {
                Text("you: \(session.lastTranscript)")
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !session.lastReply.isEmpty {
                Text("EVIE: \(session.lastReply)")
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !session.statusMessage.isEmpty {
                Text(session.statusMessage)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(session.blockers, id: \.self) { blocker in
                Text(blocker)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
