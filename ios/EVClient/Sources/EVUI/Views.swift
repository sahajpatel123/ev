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
                try queue.enqueue(payload)
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

    private func showAudit(_ memoryID: String) async {
        selected = try? await client.audit(memoryID: memoryID)
    }
}
