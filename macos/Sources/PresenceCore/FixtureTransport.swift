import Foundation

/// Fixture-replay transport: scripted event streams for checks, snapshots,
/// and the owner's offline demo. Delays pace the demo; the checks replay
/// instantly (`instant()` maps every delay to zero) so timing never
/// flakes. Cancellation ends the stream silently.
public final class FixtureTransport: PresenceTransport, @unchecked Sendable {
    public struct Step: Sendable {
        public var delayMs: UInt64
        public var event: PresenceTransportEvent
        public init(delayMs: UInt64, event: PresenceTransportEvent) {
            self.delayMs = delayMs
            self.event = event
        }
    }

    private let steps: [Step]
    private let lock = NSLock()
    private var stopped = false

    public init(steps: [Step]) {
        self.steps = steps
    }

    /// Same script, no pacing: for checks and snapshots.
    public func instant() -> FixtureTransport {
        FixtureTransport(steps: steps.map { Step(delayMs: 0, event: $0.event) })
    }

    public func stop() {
        lock.lock()
        stopped = true
        lock.unlock()
    }

    private var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }

    public func run() -> AsyncThrowingStream<PresenceTransportEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                for step in self.steps {
                    if Task.isCancelled || self.isStopped { break }
                    if step.delayMs > 0 {
                        try? await Task.sleep(nanoseconds: step.delayMs * 1_000_000)
                    }
                    if Task.isCancelled || self.isStopped { break }
                    continuation.yield(step.event)
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

// MARK: - Scripted streams (D6)

/// The five contract-level stories, as stable-id frames. Seq/ts values are
/// realistic but the store never depends on them (ordering is arrival order
/// + dedupe by id, exactly like the live stream).
public enum PresenceScripts {
    private static var seq = 0

    public struct Frame {
        var id: String
        var turnId: String?
        var type: String
        var payload: PresenceEventPayload
    }

    static func frames(_ list: [Frame], paceMs: UInt64) -> [FixtureTransport.Step] {
        [.init(delayMs: 0, event: .connection(.live(note: nil)))]
            + list.enumerated().map { index, frame in
                .init(
                    delayMs: index == 0 ? 0 : paceMs,
                    event: .event(PresenceEvent(
                        id: frame.id, seq: 40000 + index,
                        ts: "2026-10-26T19:02:11.410Z",
                        turnId: frame.turnId, type: frame.type, payload: frame.payload
                    ))
                )
            }
    }

    static func hello(_ id: String) -> Frame {
        Frame(id: id, turnId: nil, type: "stream.hello", payload: .streamHello(StreamHello(
            cursor: id, resumed: false, gap: false, restarted: false,
            capabilities: ["rich_confirm", "receipt_feed", "poll_fallback"]
        )))
    }

    // MARK: 6-step task (the contract's worked example, extended)

    public static func sixStepTask(paceMs: UInt64 = 120) -> [FixtureTransport.Step] {
        let t = "ft-sixstep"
        return frames([
            hello("pe-6-00"),
            Frame(id: "pe-6-01", turnId: t, type: "turn.started", payload: .turnStarted(
                TurnStarted(source: "gesture", label: "Listening…"))),
            Frame(id: "pe-6-02", turnId: t, type: "turn.listening", payload: .turnListening(
                TurnListening(level: 0.5, degraded: false))),
            Frame(id: "pe-6-03", turnId: t, type: "turn.transcript.final", payload: .transcriptFinal(
                TranscriptFinal(text: "Summarize yesterday's email and file the receipts.", confidence: 0.91))),
            Frame(id: "pe-6-04", turnId: t, type: "turn.thinking", payload: .turnThinking(
                TurnThinking(label: "Planning…", elapsedMs: 640))),
            Frame(id: "pe-6-05", turnId: t, type: "plan.updated", payload: .planUpdated(
                PlanUpdated(label: "Morning triage", steps: [
                    PlanStep(id: "s1", label: "Fetch unread mail", status: .active),
                    PlanStep(id: "s2", label: "Summarize threads", status: .queued),
                    PlanStep(id: "s3", label: "Find receipt attachments", status: .queued),
                    PlanStep(id: "s4", label: "File receipts", status: .queued),
                    PlanStep(id: "s5", label: "Draft replies", status: .queued),
                    PlanStep(id: "s6", label: "Report back", status: .queued),
                ]))),
            Frame(id: "pe-6-06", turnId: t, type: "tool.started", payload: .toolStarted(
                ToolStarted(callId: "c-1", stepId: "s1", name: "pim_act", label: "Fetching unread mail"))),
            Frame(id: "pe-6-07", turnId: t, type: "tool.finished", payload: .toolFinished(
                ToolFinished(callId: "c-1", stepId: "s1", name: "pim_act", label: "Mail fetched",
                             ok: true, verified: true, receiptId: "rc-1"))),
            Frame(id: "pe-6-08", turnId: t, type: "plan.updated", payload: .planUpdated(
                PlanUpdated(label: "Morning triage", steps: [
                    PlanStep(id: "s1", label: "Fetch unread mail", status: .done),
                    PlanStep(id: "s2", label: "Summarize threads", status: .active),
                    PlanStep(id: "s3", label: "Find receipt attachments", status: .queued),
                    PlanStep(id: "s4", label: "File receipts", status: .queued),
                    PlanStep(id: "s5", label: "Draft replies", status: .queued),
                    PlanStep(id: "s6", label: "Report back", status: .queued),
                ]))),
            Frame(id: "pe-6-09", turnId: t, type: "turn.progress", payload: .turnProgress(
                TurnProgress(text: "14 unread. Summarizing now.", spoken: true, sequence: 1))),
            Frame(id: "pe-6-10", turnId: t, type: "tool.started", payload: .toolStarted(
                ToolStarted(callId: "c-2", stepId: "s2", name: "briefing", label: "Summarizing threads"))),
            Frame(id: "pe-6-11", turnId: t, type: "tool.finished", payload: .toolFinished(
                ToolFinished(callId: "c-2", stepId: "s2", name: "briefing", label: "Summaries ready",
                             ok: true, verified: true, receiptId: "rc-2"))),
            Frame(id: "pe-6-12", turnId: t, type: "tool.started", payload: .toolStarted(
                ToolStarted(callId: "c-3", stepId: "s3", name: "files_op", label: "Finding receipts"))),
            Frame(id: "pe-6-13", turnId: t, type: "tool.finished", payload: .toolFinished(
                ToolFinished(callId: "c-3", stepId: "s3", name: "files_op", label: "3 receipts found",
                             ok: true, verified: true, receiptId: "rc-3"))),
            Frame(id: "pe-6-14", turnId: t, type: "plan.updated", payload: .planUpdated(
                PlanUpdated(label: "Morning triage", steps: [
                    PlanStep(id: "s1", label: "Fetch unread mail", status: .done),
                    PlanStep(id: "s2", label: "Summarize threads", status: .done),
                    PlanStep(id: "s3", label: "Find receipt attachments", status: .done),
                    PlanStep(id: "s4", label: "File receipts", status: .done),
                    PlanStep(id: "s5", label: "Draft replies", status: .done),
                    PlanStep(id: "s6", label: "Report back", status: .done),
                ]))),
            Frame(id: "pe-6-15", turnId: t, type: "turn.progress", payload: .turnProgress(
                TurnProgress(text: "All filed. Here's your morning.", spoken: true, sequence: 2))),
            Frame(id: "pe-6-16", turnId: t, type: "turn.completed", payload: .turnCompleted(
                TurnCompleted(summary: "Triaged 14 threads, filed 3 receipts.",
                              receiptIds: ["rc-1", "rc-2", "rc-3"], verified: true))),
        ], paceMs: paceMs)
    }

    // MARK: Send needing confirm (ends AT the ticket; the decision is REST)

    public static func sendNeedingConfirm(paceMs: UInt64 = 120) -> [FixtureTransport.Step] {
        let t = "ft-sendconfirm"
        return frames([
            hello("pe-c-00"),
            Frame(id: "pe-c-01", turnId: t, type: "turn.started", payload: .turnStarted(
                TurnStarted(source: "voice", label: "Listening…"))),
            Frame(id: "pe-c-02", turnId: t, type: "turn.transcript.final", payload: .transcriptFinal(
                TranscriptFinal(text: "Text Sam that I'll be late.", confidence: 0.93))),
            Frame(id: "pe-c-03", turnId: t, type: "turn.thinking", payload: .turnThinking(
                TurnThinking(label: "Thinking…", elapsedMs: 400))),
            Frame(id: "pe-c-04", turnId: t, type: "plan.updated", payload: .planUpdated(
                PlanUpdated(label: "Texting Sam", steps: [
                    PlanStep(id: "s1", label: "Find Sam's number", status: .active),
                    PlanStep(id: "s2", label: "Draft the message", status: .queued),
                    PlanStep(id: "s3", label: "Confirm with you", status: .queued),
                    PlanStep(id: "s4", label: "Send", status: .queued),
                ]))),
            Frame(id: "pe-c-05", turnId: t, type: "tool.started", payload: .toolStarted(
                ToolStarted(callId: "c-1", stepId: "s1", name: "pim_act", label: "Finding Sam"))),
            Frame(id: "pe-c-06", turnId: t, type: "tool.finished", payload: .toolFinished(
                ToolFinished(callId: "c-1", stepId: "s1", name: "pim_act", label: "Found Sam",
                             ok: true, verified: true))),
            Frame(id: "pe-c-07", turnId: t, type: "plan.updated", payload: .planUpdated(
                PlanUpdated(label: "Texting Sam", steps: [
                    PlanStep(id: "s1", label: "Find Sam's number", status: .done),
                    PlanStep(id: "s2", label: "Draft the message", status: .done),
                    PlanStep(id: "s3", label: "Confirm with you", status: .active),
                    PlanStep(id: "s4", label: "Send", status: .queued),
                ]))),
            Frame(id: "pe-c-08", turnId: t, type: "turn.needs_confirm", payload: .needsConfirm(
                NeedsConfirm(
                    ticketId: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    riskClass: "R2", channel: "message.send",
                    label: "Send via Messages to Sam Rivera?",
                    recipient: ConfirmRecipient(
                        name: "Sam Rivera", address: "+1 555-0100",
                        addressKind: "phone", firstContact: false
                    ),
                    text: "Running 10 min late — start without me?",
                    undo: UndoInfo(kind: "none", label: "Sending cannot be undone"),
                    expiresAt: "2026-10-26T19:04:11Z",
                    approve: ConfirmEndpoint(path: "/v1/fabric/approvals/{ticket_id}/approve",
                                             needsReverify: true),
                    deny: ConfirmEndpoint(path: "/v1/fabric/approvals/{ticket_id}/deny")
                ))),
        ], paceMs: paceMs)
    }

    // MARK: Failure with recovery (failed turn, then a turn that lands)

    public static func failureWithRecovery(paceMs: UInt64 = 120) -> [FixtureTransport.Step] {
        frames([
            hello("pe-f-00"),
            Frame(id: "pe-f-01", turnId: "ft-fail", type: "turn.started", payload: .turnStarted(
                TurnStarted(source: "typed", label: "Working…"))),
            Frame(id: "pe-f-02", turnId: "ft-fail", type: "turn.thinking", payload: .turnThinking(
                TurnThinking(label: "Thinking…", elapsedMs: 300))),
            Frame(id: "pe-f-03", turnId: "ft-fail", type: "tool.started", payload: .toolStarted(
                ToolStarted(callId: "c-1", name: "pim_act", label: "Finding Sam"))),
            Frame(id: "pe-f-04", turnId: "ft-fail", type: "tool.finished", payload: .toolFinished(
                ToolFinished(callId: "c-1", name: "pim_act", label: "Lookup failed",
                             ok: false, verified: false, error: "contact store unreachable"))),
            Frame(id: "pe-f-05", turnId: "ft-fail", type: "turn.failed", payload: .turnFailed(
                TurnFailed(reason: "I couldn't reach your contacts.",
                           category: "provider_error", failedStepId: "s1",
                           recoverable: true,
                           nextStep: "Check the connection, then ask again."))),
            Frame(id: "pe-f-06", turnId: "ft-retry", type: "turn.started", payload: .turnStarted(
                TurnStarted(source: "typed", label: "Working…"))),
            Frame(id: "pe-f-07", turnId: "ft-retry", type: "turn.thinking", payload: .turnThinking(
                TurnThinking(label: "Thinking…", elapsedMs: 200))),
            Frame(id: "pe-f-08", turnId: "ft-retry", type: "turn.completed", payload: .turnCompleted(
                TurnCompleted(summary: "Found Sam — ready when you are.", verified: true))),
        ], paceMs: paceMs)
    }

    // MARK: Background update (no turn; the pill stays idle)

    public static func backgroundUpdate(paceMs: UInt64 = 120) -> [FixtureTransport.Step] {
        frames([
            hello("pe-b-00"),
            Frame(id: "pe-b-01", turnId: nil, type: "background.update", payload: .backgroundUpdate(
                BackgroundUpdate(kind: "routine.fired", label: "Evening briefing is ready.",
                                 ref: BackgroundRef(type: "briefing", id: "b-42")))),
            Frame(id: "pe-b-02", turnId: nil, type: "background.update", payload: .backgroundUpdate(
                BackgroundUpdate(kind: "ticket.expired", label: "An old request expired."))),
        ], paceMs: paceMs)
    }

    // MARK: Panic (engaged, then cleared)

    public static func panic(paceMs: UInt64 = 120) -> [FixtureTransport.Step] {
        let t = "ft-panic"
        return frames([
            hello("pe-p-00"),
            Frame(id: "pe-p-01", turnId: t, type: "turn.started", payload: .turnStarted(
                TurnStarted(source: "voice", label: "Listening…"))),
            Frame(id: "pe-p-02", turnId: t, type: "turn.thinking", payload: .turnThinking(
                TurnThinking(label: "Thinking…", elapsedMs: 100))),
            Frame(id: "pe-p-03", turnId: nil, type: "panic.state", payload: .panicState(
                PanicStateEvent(panicked: true, at: "2026-10-26T19:05:00Z",
                                reason: "Owner pressed the panic hotkey."))),
            Frame(id: "pe-p-04", turnId: t, type: "turn.failed", payload: .turnFailed(
                TurnFailed(reason: "Stopped — panic is engaged.", category: "cancelled",
                           recoverable: false, nextStep: "Clear panic from the menu bar."))),
            Frame(id: "pe-p-05", turnId: nil, type: "panic.state", payload: .panicState(
                PanicStateEvent(panicked: false))),
        ], paceMs: paceMs)
    }

    public static func all() -> [(name: String, steps: [FixtureTransport.Step])] {
        [
            ("sixStepTask", sixStepTask(paceMs: 0)),
            ("sendNeedingConfirm", sendNeedingConfirm(paceMs: 0)),
            ("failureWithRecovery", failureWithRecovery(paceMs: 0)),
            ("backgroundUpdate", backgroundUpdate(paceMs: 0)),
            ("panic", panic(paceMs: 0)),
        ]
    }
}
