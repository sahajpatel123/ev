import SwiftUI
import EVClient

/// Shared SwiftUI views for the iPhone/Watch/Mac surfaces. They compile on
/// macOS (validated by `EVUIValidate`) and are imported by the app targets.

public struct HUDCardView: View {
    public let card: HUDCard
    public var onConfirm: (() -> Void)?

    public init(card: HUDCard, onConfirm: (() -> Void)? = nil) {
        self.card = card
        self.onConfirm = onConfirm
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(card.title).font(.headline)
                Spacer()
                Text(card.schemaVersion).font(.caption).foregroundStyle(.secondary)
            }
            Text(card.body).font(.body)
            if let kind = card.metaKind, !kind.isEmpty {
                Text(kind.replacingOccurrences(of: "_", with: " "))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if card.isApprovalHold, let onConfirm {
                Button("Confirm on this phone", action: onConfirm)
                    .font(.caption)
            }
            Text(String(format: "priority %.2f", card.priority))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding()
        .background(Color.gray.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

public struct TodayView: View {
    @State private var card: HUDCard?
    @State private var confirming = false
    public let client: EVAPIClient

    public init(client: EVAPIClient) {
        self.client = client
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Today").font(.title2)
            if let card {
                HUDCardView(card: card, onConfirm: card.isApprovalHold ? { confirmHold() } : nil)
            } else {
                Text("Loading…").foregroundStyle(.secondary)
            }
        }
        .padding()
        .task { await load() }
    }

    private func load() async {
        card = try? await client.hudCard()
    }

    private func confirmHold() {
        guard !confirming, let card, card.isApprovalHold else { return }
        confirming = true
        Task {
            defer { confirming = false }
            if EVLifeBiometric.isAvailable {
                let ok = await EVLifeBiometric.confirmLifeAction(
                    reason: "Confirm \(card.holdToolName ?? "this action")"
                )
                guard ok else { return }
            }
            do {
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
            } catch {
                return
            }
        }
    }
}

public struct CaptureView: View {
    @State private var text = ""
    @State private var status = ""
    public let client: EVAPIClient
    public let queue: OfflineCaptureQueue

    public init(client: EVAPIClient, queue: OfflineCaptureQueue) {
        self.client = client
        self.queue = queue
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Capture").font(.headline)
            TextEditor(text: $text)
                .frame(minHeight: 80)
                .border(Color.gray.opacity(0.3))
            HStack {
                Button("Remember") {
                    Task { await remember() }
                }
                Button("Sync queue") {
                    Task { await syncQueue() }
                }
                Spacer()
                Text(status).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding()
    }

    private func remember() async {
        let payload = CapturePayload(text: text)
        do {
            let result = try await client.capture(payload: payload)
            status = result.duplicate
                ? "duplicate — already captured"
                : "captured \(result.event?.id ?? "?")"
            text = ""
        } catch EVAPIError.transport {
            do {
                _ = try queue.enqueue(payload)
                status = "offline — queued for sync"
            } catch {
                status = "queue error: \(error)"
            }
        } catch {
            status = "capture failed: \(error)"
        }
    }

    private func syncQueue() async {
        let summary = await queue.sync(using: client)
        status = "synced \(summary.synced), dup \(summary.dropped), "
            + "quarantined \(summary.quarantined), remaining \(summary.remaining)"
    }
}

public struct MemoryBrowserView: View {
    @State private var memories: [MemoryOut] = []
    @State private var selected: AuditResponse?
    public let client: EVAPIClient

    public init(client: EVAPIClient) {
        self.client = client
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Memory browser").font(.headline)
                Spacer()
                Button("Refresh") {
                    Task { await load() }
                }
            }
            ScrollView {
                ForEach(memories, id: \.id) { memory in
                    Button {
                        Task { await showAudit(memory.id) }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(memory.memoryType) v\(memory.version)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(memory.text).multilineTextAlignment(.leading)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(4)
                    }
                    .buttonStyle(.plain)
                }
            }
            if let selected {
                Divider()
                Text("Audit: \(selected.memory.text)")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .padding()
        .task { await load() }
    }

    private func load() async {
        memories = (try? await client.memories(limit: 50))?.memories ?? []
    }

    private func showAudit(_ memoryId: String) async {
        selected = try? await client.audit(memoryId: memoryId)
    }
}

public struct ConversationView: View {
    @State private var messages: [ConversationMessage] = []
    @State private var conversationId: String?
    @State private var draft = ""
    @State private var status = ""
    public let client: EVAPIClient

    public init(client: EVAPIClient) {
        self.client = client
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Conversation").font(.headline)
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(messages, id: \.id) { message in
                        Text("\(message.role): \(message.text)")
                            .font(.body)
                            .multilineTextAlignment(.leading)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                TextField("Continue…", text: $draft)
                    .textFieldStyle(.roundedBorder)
                Button("Send") {
                    Task { await send() }
                }
            }
            Text(status).font(.caption).foregroundStyle(.secondary)
        }
        .padding()
        .task { await load() }
    }

    private func load() async {
        do {
            let detail = try await client.conversation(limit: 50)
            conversationId = detail.conversation.id
            messages = detail.messages
            status = detail.nextActions?.first ?? ""
        } catch {
            status = "conversation load failed: \(error)"
        }
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        do {
            let response = try await client.ask(text)
            conversationId = response.conversationId ?? conversationId
            status = response.reply
            await load()
        } catch {
            status = "send failed: \(error)"
        }
    }
}

public struct VoiceCaptureView: View {
    @State private var outcome = ""
    @State private var listening = false
    @State private var sessionId: String?
    @State private var utteranceText = ""
    @ObservedObject private var live: LiveVoiceCoordinator
    public let client: EVAPIClient
    public let deviceId: String

    public init(client: EVAPIClient, deviceId: String, live: LiveVoiceCoordinator? = nil) {
        self.client = client
        self.deviceId = deviceId
        _live = ObservedObject(wrappedValue: live ?? LiveVoiceCoordinator())
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Voice").font(.headline)
            if live.isRunning || live.isActive {
                liveControls
            } else {
                Button(listening ? "Listening…" : "Wake EV") {
                    Task { await wake() }
                }
                .disabled(listening)
                Button("Start live conversation") {
                    live.start(client: client, deviceId: deviceId)
                }
                Text(outcome).font(.caption).foregroundStyle(.secondary)
                if sessionId != nil {
                    HStack {
                        TextField("Speak (text fallback)…", text: $utteranceText)
                            .textFieldStyle(.roundedBorder)
                        Button("Send") {
                            Task { await speak() }
                        }
                    }
                }
            }
            Text("Opening the app is the door. Live conversation streams while EV is in the foreground.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding()
    }

    @ViewBuilder
    private var liveControls: some View {
        HStack {
            Text(live.isActive ? (live.isMuted ? "Muted" : "Live") : "Connecting…")
                .font(.caption)
            Spacer()
            Button(live.isMuted ? "Unmute" : "Mute") {
                live.toggleMute()
            }
            Button(live.cameraState.state == .active ? "Camera off" : "Camera on") {
                live.toggleCamera()
            }
            .disabled(live.cameraRequestInFlight || live.cameraState.state == .denied)
            if live.hudCard?.isApprovalHold == true {
                Button(live.confirmingHud ? "Confirming…" : "Confirm") {
                    live.confirmHold()
                }
                .disabled(live.confirmingHud)
            }
        }
        Text("Camera: \(live.cameraState.state.label)")
            .font(.caption2)
            .foregroundStyle(live.cameraState.state == .active ? .green : .secondary)
        if !live.capabilityManifest.isEmpty {
            Text("Capabilities: \(live.capabilityManifest.enabled.prefix(4).joined(separator: ", "))")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        if let card = live.hudCard {
            Text(card.title).font(.subheadline)
            Text(card.body).font(.caption).foregroundStyle(.secondary)
        }
        if let error = live.lastError, !error.isEmpty {
            Text(error).font(.caption).foregroundStyle(.orange)
        }
        if !live.transcript.isEmpty {
            Text(live.transcript).font(.caption)
        }
    }

    private func wake() async {
        listening = true
        defer { listening = false }
        do {
            let result = try await client.wakeVoice(deviceId: deviceId)
            sessionId = result.sessionId
            outcome = "state: \(result.state) · owner enrolled: \(result.ownerEnrolled)"
            if let message = result.message {
                outcome += " · \(message)"
            }
            if result.sessionId == nil {
                outcome += " · no session (enroll your voiceprint first)"
            }
        } catch {
            outcome = "wake failed: \(error)"
        }
    }

    private func speak() async {
        guard let sessionId else { return }
        let text = utteranceText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        utteranceText = ""
        do {
            let result = try await client.utterance(sessionId: sessionId, text: text)
            outcome = "EV: \(result.reply)"
        } catch {
            outcome = "utterance failed: \(error)"
        }
    }
}

public struct QueueIndicatorView: View {
    public let queue: OfflineCaptureQueue

    public init(queue: OfflineCaptureQueue) {
        self.queue = queue
    }

    public var body: some View {
        let count = (try? queue.pending().count) ?? 0
        Text(count == 0 ? "queue clear" : "\(count) offline captures pending")
            .font(.caption)
            .foregroundStyle(count == 0 ? Color.secondary : Color.orange)
    }
}

public struct AppShellView: View {
    public let client: EVAPIClient
    public let queue: OfflineCaptureQueue
    public let deviceId: String
    @ObservedObject private var live: LiveVoiceCoordinator

    public init(
        client: EVAPIClient,
        queue: OfflineCaptureQueue,
        deviceId: String = "mac-shell",
        live: LiveVoiceCoordinator? = nil
    ) {
        self.client = client
        self.queue = queue
        self.deviceId = deviceId
        _live = ObservedObject(wrappedValue: live ?? LiveVoiceCoordinator())
    }

    public var body: some View {
        TabView {
            TodayView(client: client)
                .tabItem { Label("Today", systemImage: "sun.max") }
            ConversationView(client: client)
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
            CaptureView(client: client, queue: queue)
                .tabItem { Label("Capture", systemImage: "plus.circle") }
            MemoryBrowserView(client: client)
                .tabItem { Label("Memory", systemImage: "brain") }
            VoiceCaptureView(client: client, deviceId: deviceId, live: live)
                .tabItem { Label("Voice", systemImage: "mic") }
        }
    }
}
