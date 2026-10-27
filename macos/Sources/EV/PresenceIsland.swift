import AppKit
import Combine
import EvieOrb
import PresenceCore
import SwiftUI

/// RC SEAM: the notch island hosts live Presence content.
///
/// `IslandHost` (CLI 3) owns the panel, layout and focus rules; this file
/// owns the Presence half: what content shows for a snapshot, and which orb
/// state that snapshot means. The confirm card keeps its existing panel +
/// keyboard flow (`ConfirmCardController`) — the island shows plan steps and
/// captions, and the orb badge pulses `.needsConfirm` while a ticket waits.

// MARK: - Content models

/// Observable plan list for the island's plan-steps content.
final class IslandPlanModel: ObservableObject {
    @Published var label = ""
    @Published var steps: [PlanStep] = []
}

/// Observable caption line for the island's caption content.
final class IslandCaptionModel: ObservableObject {
    @Published var text = ""
}

// MARK: - Island content adapters

/// Plan steps as island content (NSHostingView over live Presence state).
final class IslandPlanStepsContent: IslandContent {
    let view: NSView
    var contentHeight: CGFloat { CGFloat(min(steps * 22 + 8, 220)) }
    let contentID = "plan-steps"
    private var steps = 0
    private let model = IslandPlanModel()
    private var cancellables = Set<AnyCancellable>()

    init() {
        let hosted = NSHostingView(rootView: IslandPlanStepsView(model: model))
        hosted.translatesAutoresizingMaskIntoConstraints = false
        view = hosted
        model.$steps
            .sink { [weak self] steps in self?.steps = steps.count }
            .store(in: &cancellables)
    }

    func update(label: String, steps: [PlanStep]) {
        model.label = label
        model.steps = steps
        self.steps = steps.count
    }
}

private struct IslandPlanStepsView: View {
    @ObservedObject var model: IslandPlanModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !model.label.isEmpty {
                Text(model.label).font(.system(size: 13, weight: .semibold))
            }
            ForEach(model.steps, id: \.id) { step in
                HStack(spacing: 6) {
                    Circle()
                        .fill(dotColor(step.status))
                        .frame(width: 8, height: 8)
                    Text(step.label)
                        .font(.system(size: 13))
                        .foregroundColor(step.status == .queued ? .secondary : .primary)
                        .lineLimit(1)
                }
            }
        }
        .padding(4)
    }

    private func dotColor(_ status: PlanStepStatus) -> Color {
        switch status {
        case .done: return .green
        case .active: return .blue
        case .failed: return .red
        case .queued, .skipped, .other: return .secondary
        }
    }
}

/// Live caption line as island content.
final class IslandCaptionContent: IslandContent {
    let view: NSView
    let contentHeight: CGFloat = 48
    let contentID = "caption"
    private let model = IslandCaptionModel()

    init() {
        let hosted = NSHostingView(rootView: IslandCaptionView(model: model))
        hosted.translatesAutoresizingMaskIntoConstraints = false
        view = hosted
    }

    func update(text: String) {
        model.text = text
    }
}

private struct IslandCaptionView: View {
    @ObservedObject var model: IslandCaptionModel

    var body: some View {
        Text(model.text)
            .font(.system(size: 14))
            .lineLimit(2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(4)
    }
}

// MARK: - Controller

/// Drives one `IslandHost` from the `PresenceStore` snapshot.
@MainActor
final class PresenceIslandController {
    private let host = IslandHost()
    private let planContent = IslandPlanStepsContent()
    private let captionContent = IslandCaptionContent()
    private var cancellables = Set<AnyCancellable>()

    init() {
        // Audio-reactive from the existing buffers: the app dual-ingests the
        // same PCM the live path already taps (no second engine, no capture).
        host.orbView.levelSource = AtomicOrbLevelMeter.shared
        host.orbView.state = .idle
    }

    /// Show the compact orb. Called once at startup; content follows snapshots.
    func show() {
        host.window.orderFrontRegardless()
    }

    func attach(_ store: PresenceStore) {
        store.$snapshot
            .receive(on: DispatchQueue.main)
            .sink { [weak self] snapshot in self?.update(snapshot) }
            .store(in: &cancellables)
    }

    private func update(_ snapshot: PresenceSnapshot) {
        host.orbView.state = orbState(for: snapshot)
        if snapshot.panic.panicked {
            host.dismiss(animated: true)
            return
        }
        guard let turn = snapshot.turn else {
            host.dismiss(animated: true)
            return
        }
        if turn.outcome != nil {
            // Terminal bloom shows on the orb; content collapses with it.
            host.dismiss(animated: true)
            return
        }
        if !turn.steps.isEmpty {
            planContent.update(label: turn.planLabel, steps: turn.steps)
            presentIfNeeded(planContent)
        } else if let last = turn.captions.last, !last.text.isEmpty {
            captionContent.update(text: last.text)
            presentIfNeeded(captionContent)
        } else {
            host.dismiss(animated: true)
        }
    }

    private func presentIfNeeded(_ content: IslandContent) {
        if host.currentContent?.contentID != content.contentID {
            host.present(content: content, animated: true)
        } else {
            host.updateLayout(animated: false)
        }
    }

    private func orbState(for snapshot: PresenceSnapshot) -> OrbState {
        if snapshot.panic.panicked { return .error }
        if case .offline = snapshot.connection { return .offline }
        guard let turn = snapshot.turn else { return .idle }
        if turn.confirm != nil { return .needsConfirm }
        if let outcome = turn.outcome {
            switch outcome {
            case .completed: return .success
            case .failed: return .error
            }
        }
        switch turn.activity {
        case .listening: return .listening
        case .thinking: return .thinking
        case .tool: return .acting
        case .spoken: return .speaking
        case .status: return .thinking
        }
    }
}
