import SwiftUI

/// The confirm card: EXACTLY what will happen, or a disabled approve button.
///
/// Plain content view (no panel dependencies): hosts unchanged inside
/// `IslandHost` or the floating card panel. Shows recipient identity,
/// channel, the full message text (scrolls, never truncates) or a file/SQL
/// diff preview, undo availability, and risk class. Label-only tickets
/// (pre-`rich_confirm`) render the label with an honest note and a
/// disabled approve control.
public struct ConfirmCardView: View {
    @ObservedObject public var model: ConfirmCardModel

    @Environment(\.colorSchemeContrast) private var contrast

    public init(model: ConfirmCardModel) {
        self.model = model
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if model.ticket.recipient?.firstContact == true {
                firstContactBanner
            }
            recipientBlock
            payloadBlock
            undoLine
            Spacer(minLength: 0)
            footer
        }
        .padding(18)
        .frame(width: ConfirmCardMetrics.width, alignment: .leading)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(Color.orange.opacity(contrast == .increased ? 1 : 0.6),
                              lineWidth: contrast == .increased ? 2 : 1)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilitySummary)
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(ConfirmPolicy.channelName(model.ticket.channel))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)
                Spacer()
                Text(ConfirmPolicy.riskName(model.ticket.riskClass))
                    .font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(riskColor.opacity(0.18))
                    .foregroundStyle(riskColor)
                    .clipShape(Capsule())
            }
            Text(model.ticket.label.isEmpty ? "Confirm this action" : model.ticket.label)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.primary)
        }
    }

    private var riskColor: Color {
        switch model.ticket.riskClass {
        case "R0", "R1": return .green
        case "R2": return .yellow
        case "R3", "R4": return .orange
        case "forbidden": return .red
        default: return .secondary
        }
    }

    private var firstContactBanner: some View {
        HStack(spacing: 6) {
            Image(systemName: "person.crop.circle.badge.exclamationmark")
            Text("First contact — check the address carefully.")
                .font(.system(size: 12, weight: .semibold))
        }
        .foregroundStyle(.orange)
        .accessibilityLabel("Warning: first contact")
    }

    // MARK: - Recipient

    @ViewBuilder
    private var recipientBlock: some View {
        if let recipient = model.ticket.recipient {
            VStack(alignment: .leading, spacing: 2) {
                Text("TO")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                Text(recipient.name.isEmpty ? recipient.address : recipient.name)
                    .font(.system(size: 14, weight: .semibold))
                Text(addressLine(recipient))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private func addressLine(_ recipient: ConfirmRecipient) -> String {
        if recipient.address.isEmpty { return recipient.addressKind }
        if recipient.addressKind.isEmpty { return recipient.address }
        return "\(recipient.address) · \(recipient.addressKind)"
    }

    // MARK: - Payload: full text or diff, never truncated

    @ViewBuilder
    private var payloadBlock: some View {
        if let text = model.ticket.text, !text.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("MESSAGE")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                ScrollView {
                    Text(text)
                        .font(.system(size: 13))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: ConfirmCardMetrics.payloadMaxHeight)
            }
        } else if let diff = model.ticket.diff, !diff.body.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(diffTitle(diff.kind))
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if diff.truncated {
                        Text("preview truncated")
                            .font(.system(size: 10))
                            .foregroundStyle(.orange)
                    }
                }
                ScrollView {
                    Text(diff.body)
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: ConfirmCardMetrics.payloadMaxHeight)
            }
        } else if !model.ticket.isRich {
            HStack(spacing: 6) {
                Image(systemName: "info.circle")
                Text("Details unavailable — approve in the terminal instead.")
                    .font(.system(size: 12))
            }
            .foregroundStyle(.secondary)
        }
        if !model.ticket.attachments.isEmpty {
            Text("\(model.ticket.attachments.count) attachment(s): \(model.ticket.attachments.joined(separator: ", "))")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
    }

    private func diffTitle(_ kind: String) -> String {
        switch kind {
        case "unified_diff": return "DIFF PREVIEW"
        case "sql_preview": return "SQL PREVIEW"
        default: return kind.isEmpty ? "PREVIEW" : kind.uppercased()
        }
    }

    // MARK: - Undo

    private var undoLine: some View {
        HStack(spacing: 6) {
            Image(systemName: model.ticket.undo.isUndoable ? "arrow.uturn.backward" : "nosign")
            Text(model.ticket.undo.label.isEmpty
                ? (model.ticket.undo.isUndoable ? "Can be undone" : "Cannot be undone")
                : model.ticket.undo.label)
                .font(.system(size: 12))
        }
        .foregroundStyle(model.ticket.undo.isUndoable ? .green : .secondary)
    }

    // MARK: - Footer: expiry, buttons, result

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if case .failed(let message) = model.phase {
                Text(message)
                    .font(.system(size: 12))
                    .foregroundStyle(.red)
            } else if case .approved(let status) = model.phase {
                Text("Approved (\(status)).")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.green)
            } else if case .denied(let status) = model.phase {
                Text("Denied (\(status)).")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            HStack {
                if let expiry = ConfirmPolicy.expiryText(expiresAt: model.ticket.expiresAt, now: model.now) {
                    Text(expiry)
                        .font(.system(size: 11))
                        .foregroundStyle(model.expired ? .red : .secondary)
                }
                Spacer()
                Text("↵ approve · esc deny")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button("Deny") {
                    Task { await model.deny() }
                }
                .buttonStyle(.bordered)
                .controlSize(.large)
                .keyboardShortcut(.cancelAction)
                .disabled(!model.denyEnabled)
                Spacer()
                if case .working = model.phase {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("Working")
                }
                Button("Approve") {
                    Task { await model.approve() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                .disabled(!model.approveEnabled)
            }
        }
    }

    private var accessibilitySummary: String {
        var parts = [
            ConfirmPolicy.channelName(model.ticket.channel),
            model.ticket.label,
            ConfirmPolicy.riskName(model.ticket.riskClass),
        ]
        if let recipient = model.ticket.recipient {
            parts.append("To \(recipient.name) \(recipient.address)")
        }
        if let text = model.ticket.text, !text.isEmpty {
            parts.append(text)
        }
        return parts.joined(separator: ". ")
    }
}

public enum ConfirmCardMetrics {
    public static let width: CGFloat = 480
    public static let height: CGFloat = 560
    public static let payloadMaxHeight: CGFloat = 160
}
