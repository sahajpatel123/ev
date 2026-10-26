import SwiftUI

/// Rewind timeline: what Evie did. Plain content view — hosts inside the
/// menu-bar panel (next to Permissions) or any window. Today by default,
/// with search, scope, status, and verified-only filters. File rows offer
/// Restore; everything else is read-only.
public struct RewindTimelineView: View {
    @ObservedObject public var model: RewindTimelineModel
    public var onBack: () -> Void

    public init(model: RewindTimelineModel, onBack: @escaping () -> Void = {}) {
        self.model = model
        self.onBack = onBack
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            filters
            if model.loading && model.rows.isEmpty {
                HStack {
                    Spacer()
                    ProgressView("Loading…")
                    Spacer()
                }
                .padding(.vertical, 24)
            } else if let error = model.errorLine, model.rows.isEmpty {
                errorBlock(error)
            } else if model.visibleRows.isEmpty {
                emptyBlock
            } else {
                // ScrollView+VStack, not List: the AppKit table behind List
                // does not draw reliably offscreen, and rows are bounded
                // (fetch limit 100). Identical tree in app and snapshots.
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(model.visibleRows) { row in
                            TimelineRowView(
                                row: row,
                                restoring: model.restoringIds.contains(row.id),
                                onRestore: { Task { await model.restore(row) } }
                            )
                            .padding(.vertical, 6)
                            Divider()
                        }
                    }
                }
                .frame(minHeight: 240, maxHeight: 420)
            }
            if let error = model.errorLine, !model.rows.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
        .frame(width: RewindMetrics.width)
        // No .task auto-refresh: the host refreshes on open (and the button
        // refreshes later), so renders are never torn by a fetch landing
        // mid-frame — in snapshots or on screen.
    }

    private var header: some View {
        HStack {
            Button(action: onBack) {
                Image(systemName: "chevron.left")
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Back")
            Text("Rewind")
                .font(.headline)
            Spacer()
            Button("Refresh") {
                Task { await model.refresh() }
            }
            .font(.caption)
            .disabled(model.loading)
        }
    }

    private var filters: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("Search what Evie did", text: $model.search)
                .textFieldStyle(.roundedBorder)
                .font(.callout)
            HStack {
                Picker("Scope", selection: $model.scope) {
                    Text("Today").tag(TimelineScope.today)
                    Text("All").tag(TimelineScope.all)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 120)
                .accessibilityLabel("Scope")
                Picker("Status", selection: $model.statusFilter) {
                    Text("All").tag(TimelineStatusFilter.all)
                    Text("Done").tag(TimelineStatusFilter.done)
                    Text("Attention").tag(TimelineStatusFilter.attention)
                    Text("Restores").tag(TimelineStatusFilter.restores)
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .frame(width: 100)
                .accessibilityLabel("Status")
                Spacer()
                Toggle("Verified", isOn: $model.verifiedOnly)
                    .font(.caption)
            }
        }
    }

    private func errorBlock(_ error: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(.orange)
            Text(error)
                .font(.callout)
                .multilineTextAlignment(.center)
            Button("Try again") {
                Task { await model.refresh() }
            }
            .font(.caption)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
    }

    private var emptyBlock: some View {
        VStack(spacing: 6) {
            Image(systemName: "clock.arrow.circlepath")
                .foregroundStyle(.secondary)
            Text("Nothing here yet.")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("Evie's finished work lands here as she does it.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
    }
}

struct TimelineRowView: View {
    var row: TimelineRow
    var restoring: Bool
    var onRestore: () -> Void

    private static let timeFormatter: DateFormatter = {
        let fmt = DateFormatter()
        fmt.timeStyle = .short
        return fmt
    }()

    private static let dayFormatter: DateFormatter = {
        let fmt = DateFormatter()
        fmt.dateStyle = .short
        fmt.timeStyle = .none
        return fmt
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.label)
                        .font(.callout)
                        .lineLimit(2)
                    Text(row.target)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                verifiedBadge
            }
            HStack(spacing: 8) {
                Text(timeLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                statusChip
                Spacer()
                if row.undoId != nil {
                    if restoring {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Button("Undo") { onRestore() }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }
            }
            if let error = row.error, !error.isEmpty, row.kind == .action {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }
            if let result = row.resultLine {
                Text(result)
                    .font(.caption)
                    .foregroundStyle(row.resultOk == true ? .green : .red)
                    .lineLimit(2)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(row.label), \(row.target), \(row.status)")
    }

    @ViewBuilder
    private var verifiedBadge: some View {
        switch row.verified {
        case .some(true):
            Label("Verified", systemImage: "checkmark.seal.fill")
                .font(.caption)
                .foregroundStyle(.green)
        case .some(false):
            Label("Unverified", systemImage: "questionmark.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .none:
            EmptyView()
        }
    }

    private var statusChip: some View {
        Text(row.status)
            .font(.caption2)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(statusColor.opacity(0.15))
            .foregroundStyle(statusColor)
            .clipShape(Capsule())
    }

    private var statusColor: Color {
        switch row.status {
        case "executed", "approved", "restorable": return .blue
        case "failed", "denied": return .red
        case "rolled_back": return .orange
        default: return .secondary
        }
    }

    private var timeLine: String {
        guard let at = row.at else { return "—" }
        if Calendar.current.isDateInToday(at) {
            return Self.timeFormatter.string(from: at)
        }
        return "\(Self.dayFormatter.string(from: at)) \(Self.timeFormatter.string(from: at))"
    }
}

public enum RewindMetrics {
    public static let width: CGFloat = 380
    public static let height: CGFloat = 560
}
