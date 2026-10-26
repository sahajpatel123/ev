import Foundation
import PresenceCore

/// Scriptable fake decision transport. Records every call so the model
/// checks prove what hit the network (and what never did). An actor: no
/// locks, no races, reads via await.
actor FakeConfirmClient: ConfirmClient {
    struct Call: Equatable, Sendable {
        var ticketId: String
        var path: String
        var reason: String
        var reverify: String?
    }

    private(set) var approveCalls: [Call] = []
    private(set) var denyCalls: [Call] = []
    private(set) var mintCalls = 0

    var approvePlan: [Result<ConfirmDecision, ConfirmError>] = []
    var denyPlan: [Result<ConfirmDecision, ConfirmError>] = []
    var mintPlan: [Result<String, ConfirmError>] = []

    init(
        approvePlan: [Result<ConfirmDecision, ConfirmError>] = [],
        denyPlan: [Result<ConfirmDecision, ConfirmError>] = [],
        mintPlan: [Result<String, ConfirmError>] = []
    ) {
        self.approvePlan = approvePlan
        self.denyPlan = denyPlan
        self.mintPlan = mintPlan
    }

    func approve(ticketId: String, path: String, reason: String, reverify: String?) async throws -> ConfirmDecision {
        approveCalls.append(Call(ticketId: ticketId, path: path, reason: reason, reverify: reverify))
        let next = approvePlan.isEmpty ? nil : approvePlan.removeFirst()
        switch next {
        case .success(let d): return d
        case .failure(let e): throw e
        case nil: return ConfirmDecision(ticketId: ticketId, status: "approved")
        }
    }

    func deny(ticketId: String, path: String, reason: String) async throws -> ConfirmDecision {
        denyCalls.append(Call(ticketId: ticketId, path: path, reason: reason, reverify: nil))
        let next = denyPlan.isEmpty ? nil : denyPlan.removeFirst()
        switch next {
        case .success(let d): return d
        case .failure(let e): throw e
        case nil: return ConfirmDecision(ticketId: ticketId, status: "denied")
        }
    }

    func mintReverify() async throws -> String {
        mintCalls += 1
        let next = mintPlan.isEmpty ? nil : mintPlan.removeFirst()
        switch next {
        case .success(let t): return t
        case .failure(let e): throw e
        case nil: return "rv-test"
        }
    }
}

/// Card model flows against the fake: success, reverify retry, double
/// decisions, stale tickets, and every client-side refusal. Async: the
/// runner's main is async and awaits these directly.
enum ConfirmCardModelChecks {
    static func richTicket(channel: String = "message.send",
                           firstContact: Bool = false) -> NeedsConfirm {
        NeedsConfirm(
            ticketId: "t-1", riskClass: "R2", channel: channel,
            label: "Send?",
            recipient: ConfirmRecipient(
                name: "Sam", address: "+1 555-0100", addressKind: "phone",
                firstContact: firstContact
            ),
            text: "hello",
            undo: UndoInfo(kind: "none", label: "Sending cannot be undone"),
            approve: ConfirmEndpoint(path: "/v1/fabric/approvals/{ticket_id}/approve",
                                     needsReverify: true),
            deny: ConfirmEndpoint(path: "/v1/fabric/approvals/{ticket_id}/deny")
        )
    }
    static func run(_ check: CheckFn) async {
        // Approve success (master-key path: no reverify).
        do {
            let fake = FakeConfirmClient()
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approve()
            let phase = await model.phase
            let calls = await fake.approveCalls
            let mints = await fake.mintCalls
            check(
                "model-approve", phase == .approved(status: "approved")
                    && calls.count == 1 && mints == 0
                    && calls.first?.reverify == nil,
                "\(phase)"
            )
        }
        // Device-token path: 403 once, mint once, retry once with the token.
        do {
            let fake = FakeConfirmClient(approvePlan: [
                .failure(.needsReverify(message: "verify")),
                .success(ConfirmDecision(ticketId: "t-1", status: "approved")),
            ])
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approve()
            let phase = await model.phase
            let calls = await fake.approveCalls
            let mints = await fake.mintCalls
            check(
                "model-approve-reverify-retry",
                phase == .approved(status: "approved")
                    && calls.count == 2 && mints == 1
                    && calls[0].reverify == nil
                    && calls[1].reverify == "rv-test",
                "\(phase) calls=\(calls.count) mint=\(mints)"
            )
        }
        // Mint failure surfaces honestly; no second approve attempt.
        do {
            let fake = FakeConfirmClient(
                approvePlan: [.failure(.needsReverify(message: "verify"))],
                mintPlan: [.failure(.requestFailed(message: "mint down"))]
            )
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approve()
            let phase = await model.phase
            let calls = await fake.approveCalls
            if case .failed(let m) = phase {
                check("model-mint-failure", m == "mint down" && calls.count == 1, m)
            } else {
                check("model-mint-failure", false, "\(phase)")
            }
        }
        // Double-approve (409) and stale ticket (404) map to honest states.
        do {
            let fake = FakeConfirmClient(approvePlan: [
                .failure(.alreadyDecided(message: "already approved by phone")),
            ])
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approve()
            let phase = await model.phase
            if case .failed(let m) = phase {
                check("model-double-approve", m == "already approved by phone", m)
            } else {
                check("model-double-approve", false, "\(phase)")
            }
        }
        do {
            let fake = FakeConfirmClient(approvePlan: [
                .failure(.staleTicket(message: "gone")),
            ])
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approve()
            let phase = await model.phase
            if case .failed(let m) = phase {
                check("model-stale-ticket", m == "gone", m)
            } else {
                check("model-stale-ticket", false, "\(phase)")
            }
        }
        // Deny path.
        do {
            let fake = FakeConfirmClient()
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.deny()
            let phase = await model.phase
            let denies = await fake.denyCalls
            let approves = await fake.approveCalls
            check(
                "model-deny",
                phase == .denied(status: "denied") && denies.count == 1
                    && approves.isEmpty,
                "\(phase)"
            )
        }
        // Minimal ticket: approve refused WITHOUT network; deny still works.
        do {
            let fake = FakeConfirmClient()
            var minimal = richTicket()
            minimal.recipient = nil
            minimal.text = nil
            minimal.diff = nil
            let model = await ConfirmCardModel(ticket: minimal, client: fake)
            await model.approve()
            let phase = await model.phase
            let calls = await fake.approveCalls
            if case .failed(let m) = phase {
                check(
                    "model-minimal-refuses-approve",
                    m.contains("terminal") && calls.isEmpty, m
                )
            } else {
                check("model-minimal-refuses-approve", false, "\(phase)")
            }
        }
        // Expired card: neither path touches the network.
        do {
            let fake = FakeConfirmClient()
            var ticket = richTicket()
            ticket.expiresAt = "2020-01-01T00:00:00Z"
            let model = await ConfirmCardModel(ticket: ticket, client: fake)
            await model.approve()
            let expired = await model.expired
            let phase = await model.phase
            let approves = await fake.approveCalls
            let denies = await fake.denyCalls
            if case .failed = phase {
                check(
                    "model-expired-refuses",
                    expired && approves.isEmpty && denies.isEmpty,
                    "\(phase)"
                )
            } else {
                check("model-expired-refuses", false, "\(phase)")
            }
        }
        // Spoken yes: refused for HIGH_RISK, allowed for low-risk.
        do {
            let fake = FakeConfirmClient()
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approveSpoken()
            let phase = await model.phase
            let calls = await fake.approveCalls
            if case .failed(let m) = phase {
                check(
                    "model-spoken-refuses-high-risk",
                    m.contains("spoken yes") && calls.isEmpty, m
                )
            } else {
                check("model-spoken-refuses-high-risk", false, "\(phase)")
            }
        }
        do {
            let fake = FakeConfirmClient()
            let model = await ConfirmCardModel(
                ticket: richTicket(channel: "file.write"), client: fake
            )
            await model.approveSpoken()
            let phase = await model.phase
            check("model-spoken-allows-low-risk", phase == .approved(status: "approved"), "\(phase)")
        }
        do {
            let fake = FakeConfirmClient()
            let model = await ConfirmCardModel(
                ticket: richTicket(channel: "file.write", firstContact: true), client: fake
            )
            await model.approveSpoken()
            let phase = await model.phase
            let calls = await fake.approveCalls
            if case .failed = phase {
                check("model-spoken-refuses-first-contact", calls.isEmpty, "\(phase)")
            } else {
                check("model-spoken-refuses-first-contact", false, "\(phase)")
            }
        }
        // Second decision while working/finished is a no-op (no double tap).
        do {
            let fake = FakeConfirmClient()
            let model = await ConfirmCardModel(ticket: richTicket(), client: fake)
            await model.approve()
            await model.approve()
            await model.deny()
            let approves = await fake.approveCalls
            let denies = await fake.denyCalls
            check("model-single-decision", approves.count == 1 && denies.isEmpty)
        }
    }
}
