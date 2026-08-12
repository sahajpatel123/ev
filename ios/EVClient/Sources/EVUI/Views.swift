import SwiftUI
import EVClient

/// Shared SwiftUI views for the iPhone/Watch/Mac surfaces. They compile on
/// macOS (validated by `EVUIValidate`) and are imported by the app targets.

public struct HUDCardView: View {
    public let card: HUDCard

    public init(card: HUDCard) {
        self.card = card
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(card.title).font(.headline)
                Spacer()
                Text(card.schemaVersion).font(.caption).foregroundStyle(.secondary)
            }
            Text(card.body).font(.body)
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
    public let client: EVAPIClient

    public init(client: EVAPIClient) {
        self.client = client
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Today").font(.title2)
            if let card {
                HUDCardView(card: card)
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
    public let client: EVAPIClient
    public let deviceId: String

    public init(client: EVAPIClient, deviceId: String) {
        self.client = client
        self.deviceId = deviceId
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Voice").font(.headline)
            Button(listening ? "Listening…" : "Wake EV") {
                Task { await wake() }
            }
            .disabled(listening)
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
            Text("Mic capture needs the iOS app target and a permission grant.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding()
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

    public init(client: EVAPIClient, queue: OfflineCaptureQueue) {
        self.client = client
        self.queue = queue
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
            VoiceCaptureView(client: client, deviceId: "mac-shell")
                .tabItem { Label("Voice", systemImage: "mic") }
        }
    }
}
