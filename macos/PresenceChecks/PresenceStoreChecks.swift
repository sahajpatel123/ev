import Foundation
import PresenceCore

/// Reducer checks: deterministic snapshot transitions for scripted event
/// sequences, plus the pill-priority rules.
enum PresenceStoreChecks {
    static var nextId = 0
    static func ev(
        _ payload: PresenceEventPayload, turnId: String? = "ft-1",
        id: String? = nil
    ) -> PresenceTransportEvent {
        nextId += 1
        let type: String
        switch payload {
        case .streamHello: type = "stream.hello"
        case .turnStarted: type = "turn.started"
        case .turnListening: type = "turn.listening"
        case .transcriptPartial: type = "turn.transcript.partial"
        case .transcriptFinal: type = "turn.transcript.final"
        case .turnThinking: type = "turn.thinking"
        case .planUpdated: type = "plan.updated"
        case .toolStarted: type = "tool.started"
        case .toolFinished: type = "tool.finished"
        case .lookingAt: type = "ambient.looking_at"
        case .needsConfirm: type = "turn.needs_confirm"
        case .turnProgress: type = "turn.progress"
        case .turnCompleted: type = "turn.completed"
        case .turnFailed: type = "turn.failed"
        case .backgroundUpdate: type = "background.update"
        case .panicState: type = "panic.state"
        case .unknown(let t): type = t
        }
        return .event(PresenceEvent(
            id: id ?? "pe-\(nextId)", turnId: turnId, type: type, payload: payload
        ))
    }

    static func run(_ check: CheckFn) {
        // Full turn lifecycle: idle → listening → thinking → acting →
        // confirm → completed; then a new turn resets.
        do {
            var s = PresenceSnapshot()
            check("store-idle", s.pill == .idle, "\(s.pill)")
            s.apply(.connection(.live(note: nil)))
            s.apply(ev(.turnStarted(TurnStarted(source: "gesture", label: "Listening…"))))
            s.apply(ev(.turnListening(TurnListening(level: 0.5, degraded: false))))
            if case .listening(let level, let degraded) = s.pill {
                check("store-listening", level == 0.5 && !degraded, "\(s.pill)")
            } else {
                check("store-listening", false, "\(s.pill)")
            }
            s.apply(ev(.transcriptFinal(TranscriptFinal(text: "Text Sam.", confidence: 0.9))))
            check("store-transcript", s.turn?.transcript == "Text Sam." && s.turn?.transcriptFinal == true)
            s.apply(ev(.turnThinking(TurnThinking(label: "Thinking…", elapsedMs: 100))))
            if case .thinking = s.pill {
                check("store-thinking", true)
            } else {
                check("store-thinking", false, "\(s.pill)")
            }
            s.apply(ev(.planUpdated(PlanUpdated(label: "Texting Sam", steps: [
                PlanStep(id: "s1", label: "Find Sam", status: .done),
                PlanStep(id: "s2", label: "Confirm", status: .active),
                PlanStep(id: "s3", label: "Send", status: .queued),
            ]))))
            check("store-plan", s.turn?.activeStep?.id == "s2" && s.turn?.steps.count == 3)
            s.apply(ev(.toolStarted(ToolStarted(callId: "c1", name: "comms_act", label: "Drafting message"))))
            if case .acting(let label) = s.pill {
                check("store-acting", label == "Drafting message", "\(s.pill)")
            } else {
                check("store-acting", false, "\(s.pill)")
            }
            s.apply(ev(.toolFinished(ToolFinished(
                callId: "c1", name: "comms_act", label: "Draft ready",
                ok: true, verified: true, receiptId: "rc-1"
            ))))
            check(
                "store-tool-outcome", s.turn?.lastTool?.verified == true
                    && s.turn?.activeToolLabel == nil
            )
            s.apply(ev(.turnProgress(TurnProgress(text: "Found Sam.", spoken: true, sequence: 1))))
            if case .speaking(let caption) = s.pill {
                check("store-speaking", caption == "Found Sam.", "\(s.pill)")
            } else {
                check("store-speaking", false, "\(s.pill)")
            }
            // A non-spoken status line must not displace the spoken activity.
            s.apply(ev(.turnProgress(TurnProgress(text: "status note", spoken: false, sequence: 2))))
            if case .speaking = s.pill {
                check("store-status-no-displace", true)
            } else {
                check("store-status-no-displace", false, "\(s.pill)")
            }
            check("store-captions-cap", s.turn?.captions.count == 2)
            s.apply(ev(.needsConfirm(NeedsConfirm(ticketId: "t1", riskClass: "R2",
                                                  channel: "message.send", label: "Send?"))))
            if case .needsConfirm = s.pill {
                check("store-confirm-priority", true)
            } else {
                check("store-confirm-priority", false, "\(s.pill)")
            }
            s.apply(ev(.turnCompleted(TurnCompleted(summary: "Sent.", verified: true))))
            if case .completed(let label) = s.pill {
                check("store-completed", label == "Sent." && s.turn?.confirm == nil, "\(s.pill)")
            } else {
                check("store-completed", false, "\(s.pill)")
            }
            // Terminal persists until the next turn starts.
            s.apply(ev(.turnProgress(TurnProgress(text: "late line", spoken: true, sequence: 9))))
            if case .completed = s.pill {
                check("store-terminal-persists", true)
            } else {
                check("store-terminal-persists", false, "\(s.pill)")
            }
            s.apply(ev(.turnStarted(TurnStarted(source: "hotkey", label: "Listening…")), turnId: "ft-2"))
            check(
                "store-new-turn-resets",
                s.turn?.id == "ft-2" && s.turn?.outcome == nil && s.turn?.captions.isEmpty == true,
                "\(String(describing: s.turn?.id))"
            )
        }

        // Failure path: honest reason + next step, confirm cleared.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.turnStarted(TurnStarted(source: "voice", label: "Listening…"))))
            s.apply(ev(.needsConfirm(NeedsConfirm(ticketId: "t9", label: "Send?"))))
            s.apply(ev(.turnFailed(TurnFailed(
                reason: "No number on file.", category: "missing_data",
                recoverable: true, nextStep: "Add the number."
            ))))
            if case .failed(let reason, let next) = s.pill {
                check("store-failed", reason == "No number on file." && next == "Add the number.")
            } else {
                check("store-failed", false, "\(s.pill)")
            }
            check("store-failed-clears-confirm", s.turn?.confirm == nil)
        }

        // At-least-once replays dedupe by id.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.turnStarted(TurnStarted(source: "voice", label: "x")), id: "pe-dup"))
            s.apply(ev(.turnProgress(TurnProgress(text: "one", spoken: true, sequence: 1)), id: "pe-dup"))
            check(
                "store-dedupe",
                s.turn?.id == "ft-1" && s.turn?.captions.isEmpty == true
                    && s.turn?.source == "voice",
                "captions=\(s.turn?.captions.count ?? -1)"
            )
        }

        // Panic is orthogonal state: set, surfaced, cleared.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.turnStarted(TurnStarted(source: "voice", label: "x"))))
            s.apply(ev(.panicState(PanicStateEvent(panicked: true, reason: "Hotkey."))))
            check("store-panic-set", s.panic.panicked && s.panic.reason == "Hotkey.")
            if case .listening = s.pill {
                check("store-panic-no-replace", true)
            } else {
                check("store-panic-no-replace", false, "\(s.pill)")
            }
            s.apply(ev(.panicState(PanicStateEvent(panicked: false))))
            check("store-panic-clear", !s.panic.panicked)
        }

        // Restart drops the turn and the dedupe window.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.turnStarted(TurnStarted(source: "voice", label: "x")), id: "pe-r1"))
            s.apply(ev(.streamHello(StreamHello(restarted: true, capabilities: ["rich_confirm"])),
                       turnId: nil, id: "pe-r2"))
            check(
                "store-restart",
                s.turn == nil && s.seenIds.isEmpty && s.hasRichConfirm,
                "turn=\(String(describing: s.turn)) seen=\(s.seenIds.count)"
            )
        }

        // Gap flag + capabilities recorded; unknown events ignored.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.streamHello(StreamHello(resumed: false, gap: true)), turnId: nil))
            check("store-gap", s.streamGap && !s.hasRichConfirm)
            s.apply(ev(.unknown(type: "turn.hologram")))
            check("store-unknown-ignored", s.turn == nil && s.streamGap)
        }

        // Background notes cap at 5, newest last.
        do {
            var s = PresenceSnapshot()
            for i in 1...7 {
                s.apply(ev(.backgroundUpdate(BackgroundUpdate(kind: "sync.note", label: "n\(i)")),
                           turnId: nil))
            }
            check(
                "store-background-cap",
                s.backgroundNotes.count == 5 && s.backgroundNotes.last?.label == "n7"
                    && s.backgroundNotes.first?.label == "n3",
                "\(s.backgroundNotes.map(\.label))"
            )
        }

        // Captions cap at 3.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.turnStarted(TurnStarted(source: "voice", label: "x"))))
            for i in 1...5 {
                s.apply(ev(.turnProgress(TurnProgress(text: "l\(i)", spoken: true, sequence: i))))
            }
            check(
                "store-caption-cap",
                s.turn?.captions.count == 3 && s.turn?.captions.last?.text == "l5",
                "\(s.turn?.captions.map(\.text) ?? [])"
            )
        }

        // Looking-at flows through; connection notes recorded.
        do {
            var s = PresenceSnapshot()
            s.apply(ev(.lookingAt(LookingAt(app: "Mail", window: "Inbox", bundleId: "x")), turnId: nil))
            check("store-looking-at", s.lookingAt.app == "Mail")
            s.apply(.connection(.reconnecting(note: "Back in a moment")))
            check("store-connection", s.connection.note == "Back in a moment" && !s.connection.isLive)
        }

        // Sensitive apps blank the chip (pure rule, no live NSWorkspace).
        do {
            let blank = LookingAtMonitor.label(
                app: "1Password", window: "Vault", bundleId: "com.1password.1password"
            )
            let shown = LookingAtMonitor.label(
                app: "Mail", window: "Inbox", bundleId: "com.apple.mail"
            )
            let userBlank = LookingAtMonitor.label(
                app: "Bank", window: "Accounts", bundleId: "com.example.bank",
                userExcluded: ["com.example.bank"]
            )
            check(
                "looking-sensitive-blank",
                blank.isEmpty && !shown.isEmpty && userBlank.isEmpty,
                "\(blank) \(shown) \(userBlank)"
            )
        }
    }
}
