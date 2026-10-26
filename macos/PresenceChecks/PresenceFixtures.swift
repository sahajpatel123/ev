import Foundation
import PresenceCore

/// Named presence snapshots built through the real reducer path. Shared by
/// the HUD (D3), confirm (D4), timeline (D5), and replay (D6) suites so every
/// PNG renders a state the event stream can actually produce.
enum PresenceFixtures {
    typealias E = PresenceStoreChecks

    static func listening(level: Double = 0.6, degraded: Bool = false) -> PresenceSnapshot {
        var s = PresenceSnapshot()
        s.apply(.connection(.live(note: nil)))
        s.apply(E.ev(.turnStarted(TurnStarted(source: "gesture", label: "Listening…"))))
        s.apply(E.ev(.turnListening(TurnListening(level: level, degraded: degraded))))
        return s
    }

    static func thinking() -> PresenceSnapshot {
        var s = listening(level: 0.2)
        s.apply(E.ev(.transcriptFinal(TranscriptFinal(text: "Text Sam that I'll be late.", confidence: 0.93))))
        s.apply(E.ev(.turnThinking(TurnThinking(label: "Thinking…", elapsedMs: 640))))
        return s
    }

    static func acting() -> PresenceSnapshot {
        var s = thinking()
        s.apply(E.ev(.planUpdated(PlanUpdated(label: "Texting Sam", steps: [
            PlanStep(id: "s1", label: "Find Sam's number", status: .done),
            PlanStep(id: "s2", label: "Draft the message", status: .done),
            PlanStep(id: "s3", label: "Confirm with you", status: .active),
            PlanStep(id: "s4", label: "Send", status: .queued),
        ]))))
        s.apply(E.ev(.turnProgress(TurnProgress(text: "Found Sam. Drafting your message.", spoken: true, sequence: 1))))
        s.apply(E.ev(.toolStarted(ToolStarted(
            callId: "c-2", stepId: "s2", name: "comms_act",
            label: "Drafting message", target: "Sam (Messages)"
        ))))
        s.apply(E.ev(.lookingAt(LookingAt(app: "Messages", window: "Sam Rivera", bundleId: "com.apple.MobileSMS")),
                     turnId: nil))
        return s
    }

    static func needsConfirm() -> PresenceSnapshot {
        var s = acting()
        s.apply(E.ev(.toolFinished(ToolFinished(
            callId: "c-2", name: "comms_act", label: "Draft ready",
            ok: true, verified: true, receiptId: "rc-11"
        ))))
        s.apply(E.ev(.needsConfirm(NeedsConfirm(
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
            approve: ConfirmEndpoint(path: "/v1/fabric/approvals/t/approve", needsReverify: true),
            deny: ConfirmEndpoint(path: "/v1/fabric/approvals/t/deny")
        ))))
        return s
    }

    static func speaking() -> PresenceSnapshot {
        var s = acting()
        s.apply(E.ev(.turnProgress(TurnProgress(text: "Draft is ready for your review.", spoken: true, sequence: 2))))
        return s
    }

    static func completed() -> PresenceSnapshot {
        var s = needsConfirm()
        s.apply(E.ev(.toolStarted(ToolStarted(callId: "c-3", name: "comms_act", label: "Sending"))))
        s.apply(E.ev(.toolFinished(ToolFinished(
            callId: "c-3", name: "comms_act", label: "Sent",
            ok: true, verified: true, receiptId: "rc-77"
        ))))
        s.apply(E.ev(.turnCompleted(TurnCompleted(
            summary: "Sent to Sam via Messages.", receiptIds: ["rc-77"], verified: true
        ))))
        return s
    }

    static func failed() -> PresenceSnapshot {
        var s = thinking()
        s.apply(E.ev(.turnFailed(TurnFailed(
            reason: "Sam has no phone number on file.",
            category: "missing_data", failedStepId: "s1",
            recoverable: true,
            nextStep: "Add Sam's number to Contacts, then ask again."
        ))))
        return s
    }

    static func panicked() -> PresenceSnapshot {
        var s = acting()
        s.apply(E.ev(.panicState(PanicStateEvent(
            panicked: true, at: "2026-10-26T19:05:00Z", reason: "Owner pressed the panic hotkey."
        )), turnId: nil))
        return s
    }

    static func reconnecting() -> PresenceSnapshot {
        var s = thinking()
        s.apply(.connection(.reconnecting(note: "Connection lost — replaying missed events…")))
        return s
    }
}
