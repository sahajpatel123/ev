import SwiftUI

/// The presence pill: one glanceable bar for the current turn, driven only
/// by `PresenceSnapshot` (which is driven only by contract events).
///
/// States: idle renders nothing (the controller hides the panel);
/// listening shows a live level meter; thinking/acting show the current
/// label; needs-confirm offers Review; speaking shows the live caption;
/// completed/failed show the outcome with Dismiss. Plan steps and captions
/// render in the expanded section; the looking-at chip is always visible
/// when Evie is attending to an app.
public struct HUDPillView: View {
    public var snapshot: PresenceSnapshot
    public var captionsEnabled: Bool
    public var expanded: Bool
    public var onToggleExpanded: () -> Void
    public var onReviewConfirm: () -> Void
    public var onDismiss: () -> Void
    /// Snapshot-test override (the live view always reads the environment).
    /// The SDK exposes this environment key read-only, so the variant
    /// injects the same value the system would provide through this input.
    /// (No Reduce Motion override exists: presence views are fully static —
    /// all motion belongs to CLI 3's orb — so Reduce Motion holds trivially.)
    public var contrastOverride: ColorSchemeContrast?

    @Environment(\.colorSchemeContrast) private var contrast

    private var effectiveContrast: ColorSchemeContrast { contrastOverride ?? contrast }

    public init(
        snapshot: PresenceSnapshot,
        captionsEnabled: Bool = true,
        expanded: Bool = false,
        onToggleExpanded: @escaping () -> Void = {},
        onReviewConfirm: @escaping () -> Void = {},
        onDismiss: @escaping () -> Void = {},
        contrastOverride: ColorSchemeContrast? = nil
    ) {
        self.snapshot = snapshot
        self.captionsEnabled = captionsEnabled
        self.expanded = expanded
        self.onToggleExpanded = onToggleExpanded
        self.onReviewConfirm = onReviewConfirm
        self.onDismiss = onDismiss
        self.contrastOverride = contrastOverride
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if snapshot.panic.panicked {
                panicBanner
            }
            statusRow
            if expanded {
                expandedSection
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(width: HUDPillMetrics.width, alignment: .leading)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(borderColor, lineWidth: effectiveContrast == .increased ? 2 : 1)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilitySummary)
    }

    private var borderColor: Color {
        switch snapshot.pill {
        case .failed: return .red.opacity(effectiveContrast == .increased ? 1 : 0.6)
        case .needsConfirm: return .orange.opacity(effectiveContrast == .increased ? 1 : 0.6)
        default: return .white.opacity(effectiveContrast == .increased ? 0.6 : 0.18)
        }
    }

    // MARK: - Status row

    private var statusRow: some View {
        HStack(spacing: 10) {
            OrbSlotView()
            statusSymbol
            VStack(alignment: .leading, spacing: 2) {
                Text(primaryLine)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(2)
                    .foregroundStyle(.primary)
                if let secondary = secondaryLine {
                    Text(secondary)
                        .font(.system(size: 11))
                        .lineLimit(1)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 4)
            if !snapshot.lookingAt.isEmpty {
                lookingChip
            }
            controls
        }
    }

    @ViewBuilder
    private var statusSymbol: some View {
        switch snapshot.pill {
        case .idle:
            EmptyView()
        case .listening:
            // Static by design: the animated orb and all listening motion
            // belong to CLI 3 (evie-orb). Presence renders state, not motion.
            Image(systemName: "mic")
                .foregroundStyle(.secondary)
                .accessibilityLabel("Listening")
        case .thinking:
            Image(systemName: "sparkles")
                .foregroundStyle(.orange)
        case .acting:
            Image(systemName: "gearshape")
                .foregroundStyle(.blue)
        case .needsConfirm:
            Image(systemName: "exclamationmark.shield")
                .foregroundStyle(.orange)
        case .speaking:
            Image(systemName: "waveform")
                .foregroundStyle(.purple)
        case .completed:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        }
    }

    private var primaryLine: String {
        switch snapshot.pill {
        case .idle: return ""
        case .listening: return "Listening…"
        case .thinking(let label): return label
        case .acting(let label): return label
        case .needsConfirm(let label): return label.isEmpty ? "Needs your decision" : label
        case .speaking(let caption): return caption.isEmpty ? "Speaking…" : caption
        case .completed(let label): return label.isEmpty ? "Done" : label
        case .failed(let reason, _): return reason.isEmpty ? "Something failed" : reason
        }
    }

    private var secondaryLine: String? {
        switch snapshot.pill {
        case .failed(_, let nextStep):
            return nextStep.isEmpty ? nil : nextStep
        case .needsConfirm:
            return "↵ review · esc deny"
        default:
            if captionsEnabled, let last = snapshot.turn?.captions.last,
               !last.text.isEmpty, !matchesPrimary(last.text) {
                return last.text
            }
            return snapshot.turn?.transcriptFinal == true
                ? snapshot.turn?.transcript : nil
        }
    }

    private func matchesPrimary(_ text: String) -> Bool {
        text == primaryLine
    }

    // MARK: - Looking-at chip

    private var lookingChip: some View {
        HStack(spacing: 4) {
            Image(systemName: "eye")
                .font(.system(size: 10))
            Text(chipLabel)
                .font(.system(size: 10))
                .lineLimit(1)
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(.quaternary.opacity(0.5))
        .clipShape(Capsule())
        .accessibilityLabel("Looking at \(chipLabel)")
    }

    private var chipLabel: String {
        let app = snapshot.lookingAt.app
        let window = snapshot.lookingAt.window
        if app.isEmpty { return window }
        if window.isEmpty { return app }
        return "\(app) — \(window)"
    }

    // MARK: - Controls (the only clickable regions; the rest is click-through)

    @ViewBuilder
    private var controls: some View {
        switch snapshot.pill {
        case .needsConfirm:
            Button("Review", action: onReviewConfirm)
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
        case .completed, .failed:
            Button(action: onDismiss) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .accessibilityLabel("Dismiss")
        default:
            if !(snapshot.turn?.steps.isEmpty ?? true) {
                Button(action: onToggleExpanded) {
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel(expanded ? "Hide plan" : "Show plan")
            }
        }
    }

    // MARK: - Expanded section

    @ViewBuilder
    private var expandedSection: some View {
        if let turn = snapshot.turn, !turn.steps.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                if !turn.planLabel.isEmpty {
                    Text(turn.planLabel)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                ForEach(turn.steps, id: \.id) { step in
                    HStack(spacing: 6) {
                        stepSymbol(step.status)
                        Text(step.label)
                            .font(.system(size: 12))
                            .lineLimit(1)
                            .foregroundStyle(step.status == .active ? .primary : .secondary)
                    }
                }
            }
            Divider()
        }
        if captionsEnabled, let turn = snapshot.turn, !turn.captions.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(turn.captions.enumerated()), id: \.offset) { _, caption in
                    Text(caption.text)
                        .font(.system(size: 11))
                        .italic(!caption.spoken)
                        .lineLimit(2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        if let note = snapshot.connection.note, !snapshot.connection.isLive {
            Text(note)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        }
    }

    private func stepSymbol(_ status: PlanStepStatus) -> some View {
        Group {
            switch status {
            case .done:
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .active:
                Image(systemName: "arrow.right.circle.fill").foregroundStyle(.blue)
            case .failed:
                Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
            case .skipped:
                Image(systemName: "minus.circle").foregroundStyle(.secondary)
            case .queued:
                Image(systemName: "circle").foregroundStyle(.secondary)
            case .other:
                Image(systemName: "questionmark.circle").foregroundStyle(.secondary)
            }
        }
        .font(.system(size: 11))
    }

    // MARK: - Panic banner

    private var panicBanner: some View {
        HStack(spacing: 6) {
            Image(systemName: "pause.octagon.fill")
                .foregroundStyle(.red)
            Text(panicText)
                .font(.system(size: 11, weight: .semibold))
                .lineLimit(2)
        }
        .accessibilityLabel("Panic: \(panicText)")
    }

    private var panicText: String {
        if let reason = snapshot.panic.reason, !reason.isEmpty {
            return "Paused — \(reason)"
        }
        return "Paused"
    }

    private var accessibilitySummary: String {
        var parts = [primaryLine]
        if let secondary = secondaryLine { parts.append(secondary) }
        if !snapshot.lookingAt.isEmpty { parts.append("Looking at \(chipLabel)") }
        return parts.joined(separator: ". ")
    }
}

// MARK: - Orb slot (CLI 3 insertion point)

/// Fixed-size placeholder where CLI 3's animated orb (`EvieOrbView`) goes.
/// Presence never draws its own orb or listening animation: this slot keeps
/// the layout stable until the island/orb branch lands, and the same content
/// views host unchanged inside `IslandHost` or the floating panel.
public struct OrbSlotView: View {
    public static let size: CGFloat = 28

    public init() {}

    public var body: some View {
        Circle()
            .strokeBorder(Color.secondary.opacity(0.35), lineWidth: 1.5)
            .frame(width: Self.size, height: Self.size)
            .accessibilityHidden(true)
    }
}

// MARK: - Metrics

public enum HUDPillMetrics {
    public static let width: CGFloat = 440
    public static let compactHeight: CGFloat = 76
    public static let expandedHeight: CGFloat = 320
}
