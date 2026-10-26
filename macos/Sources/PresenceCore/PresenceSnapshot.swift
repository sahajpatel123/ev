import Foundation

/// Pure reducer: contract events in, HUD-ready snapshot out. No clock, no IO,
/// no singletons — every transition is a deterministic function of the last
/// snapshot plus one transport event, which is what the checks drive.
public struct PresenceSnapshot: Sendable, Equatable {
    public static let maxCaptions = 3
    public static let maxBackgroundNotes = 5
    public static let maxSeenIds = 500

    public var connection: PresenceConnectionState = .offline(note: "Not connected")
    public var serverContract: String?
    public var capabilities: [String] = []
    public var streamGap: Bool = false
    public var turn: PresenceTurnState?
    public var lookingAt = LookingAt()
    public var panic = PanicStateEvent()
    public var backgroundNotes: [BackgroundUpdate] = []
    /// Dedupe ring for at-least-once delivery (contract §2). Newest last.
    public var seenIds: [String] = []

    public init() {}

    public var hasRichConfirm: Bool { capabilities.contains("rich_confirm") }

    public mutating func apply(_ transportEvent: PresenceTransportEvent) {
        switch transportEvent {
        case .connection(let state):
            connection = state
        case .event(let event):
            if seenIds.contains(event.id) { return }
            seenIds.append(event.id)
            if seenIds.count > Self.maxSeenIds {
                seenIds.removeFirst(seenIds.count - Self.maxSeenIds)
            }
            applyEvent(event)
        }
    }

    private mutating func applyEvent(_ event: PresenceEvent) {
        switch event.payload {
        case .streamHello(let hello):
            serverContract = hello.contract
            capabilities = hello.capabilities
            streamGap = hello.gap
            if hello.restarted {
                // New server process: cursors and latch-era state are stale.
                seenIds.removeAll()
                turn = nil
            }
        case .turnStarted(let started):
            turn = PresenceTurnState(
                id: event.turnId ?? event.id,
                source: started.source,
                label: started.label,
                preview: started.utterancePreview
            )
        case .turnListening(let listening):
            turn?.level = listening.level
            turn?.levelDegraded = listening.degraded
            turn?.activity = .listening
        case .transcriptPartial(let partial):
            turn?.transcript = partial.text
            turn?.transcriptFinal = false
            turn?.confidence = partial.confidence
        case .transcriptFinal(let final):
            turn?.transcript = final.text
            turn?.transcriptFinal = true
            turn?.confidence = final.confidence
        case .turnThinking(let thinking):
            turn?.activity = .thinking(label: thinking.label)
        case .planUpdated(let plan):
            turn?.planLabel = plan.label
            turn?.steps = plan.steps
        case .toolStarted(let tool):
            turn?.activity = .tool(label: tool.label, name: tool.name, target: tool.target)
            turn?.activeToolLabel = tool.label
        case .toolFinished(let tool):
            turn?.activeToolLabel = nil
            turn?.lastTool = PresenceToolOutcome(
                label: tool.label, ok: tool.ok, verified: tool.verified,
                error: tool.error, receiptId: tool.receiptId, undo: tool.undo
            )
            if !tool.ok {
                turn?.activity = .status(text: tool.label)
            }
        case .lookingAt(let looking):
            lookingAt = looking
        case .needsConfirm(let confirm):
            turn?.confirm = confirm
        case .turnProgress(let progress):
            var captions = turn?.captions ?? []
            captions.append(PresenceCaption(text: progress.text, spoken: progress.spoken))
            if captions.count > Self.maxCaptions {
                captions.removeFirst(captions.count - Self.maxCaptions)
            }
            turn?.captions = captions
            if progress.spoken {
                turn?.activity = .spoken(text: progress.text)
            } else if turn?.activity.isPassive == true {
                turn?.activity = .status(text: progress.text)
            }
        case .turnCompleted(let completed):
            turn?.outcome = .completed(summary: completed.summary, verified: completed.verified)
            turn?.confirm = nil
            turn?.activeToolLabel = nil
        case .turnFailed(let failed):
            turn?.outcome = .failed(reason: failed.reason, nextStep: failed.nextStep)
            turn?.confirm = nil
            turn?.activeToolLabel = nil
        case .backgroundUpdate(let update):
            backgroundNotes.append(update)
            if backgroundNotes.count > Self.maxBackgroundNotes {
                backgroundNotes.removeFirst(backgroundNotes.count - Self.maxBackgroundNotes)
            }
        case .panicState(let panicState):
            panic = panicState
        case .unknown:
            break // additive rule: ignore, stay connected
        }
    }
}

public struct PresenceCaption: Sendable, Equatable {
    public var text: String
    public var spoken: Bool
    public init(text: String, spoken: Bool) {
        self.text = text; self.spoken = spoken
    }
}

public struct PresenceToolOutcome: Sendable, Equatable {
    public var label: String
    public var ok: Bool
    public var verified: Bool
    public var error: String?
    public var receiptId: String?
    public var undo: UndoInfo
    public init(label: String, ok: Bool, verified: Bool, error: String? = nil,
                receiptId: String? = nil, undo: UndoInfo = .init()) {
        self.label = label; self.ok = ok; self.verified = verified
        self.error = error; self.receiptId = receiptId; self.undo = undo
    }
}

/// Latest foreground activity: what the pill's primary line shows when no
/// terminal outcome or confirm ticket takes precedence.
public enum PresenceActivity: Sendable, Equatable {
    case listening
    case thinking(label: String)
    case tool(label: String, name: String, target: String?)
    case spoken(text: String)
    case status(text: String)

    /// Statuses may be superseded by later status lines, but never overwrite
    /// a tool/thinking activity the turn is still inside.
    var isPassive: Bool {
        switch self {
        case .listening, .status: return true
        case .thinking, .tool, .spoken: return false
        }
    }
}

public enum PresenceOutcome: Sendable, Equatable {
    case completed(summary: String, verified: Bool)
    case failed(reason: String, nextStep: String)
}

public struct PresenceTurnState: Sendable, Equatable {
    public var id: String
    public var source: String
    public var label: String
    public var preview: String
    public var transcript: String = ""
    public var transcriptFinal: Bool = false
    public var confidence: Double = 0
    public var level: Double = 0
    public var levelDegraded: Bool = false
    public var activity: PresenceActivity = .listening
    public var planLabel: String = ""
    public var steps: [PlanStep] = []
    public var activeToolLabel: String?
    public var lastTool: PresenceToolOutcome?
    public var captions: [PresenceCaption] = []
    public var confirm: NeedsConfirm?
    public var outcome: PresenceOutcome?

    public init(id: String, source: String, label: String, preview: String) {
        self.id = id; self.source = source; self.label = label; self.preview = preview
    }

    public var activeStep: PlanStep? {
        steps.first { $0.status == .active }
    }
}

/// What the pill renders. Priority: failed > needs-confirm > completed >
/// activity. Panic is a banner alongside, never a replacement.
public enum PillDisplay: Sendable, Equatable {
    case idle
    case listening(level: Double, degraded: Bool)
    case thinking(label: String)
    case acting(label: String)
    case needsConfirm(label: String)
    case speaking(caption: String)
    case completed(label: String)
    case failed(reason: String, nextStep: String)
}

extension PresenceSnapshot {
    public var pill: PillDisplay {
        guard let turn else { return .idle }
        if let outcome = turn.outcome {
            switch outcome {
            case .failed(let reason, let nextStep):
                return .failed(reason: reason, nextStep: nextStep)
            case .completed(let summary, _):
                return .completed(label: summary.isEmpty ? turn.planLabel : summary)
            }
        }
        if let confirm = turn.confirm {
            return .needsConfirm(label: confirm.label)
        }
        switch turn.activity {
        case .listening:
            return .listening(level: turn.level, degraded: turn.levelDegraded)
        case .thinking(let label):
            return .thinking(label: label.isEmpty ? "Thinking…" : label)
        case .tool(let label, _, _):
            return .acting(label: label)
        case .spoken(let text):
            return .speaking(caption: text)
        case .status(let text):
            if let tool = turn.activeToolLabel {
                return .acting(label: tool)
            }
            return .thinking(label: text.isEmpty ? "Working…" : text)
        }
    }
}
