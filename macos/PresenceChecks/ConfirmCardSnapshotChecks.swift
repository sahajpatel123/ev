import AppKit
import Foundation
import PresenceCore
import SwiftUI

/// D4 snapshot suite: every confirm-card state → offscreen PNG (light +
/// dark). Result states are driven through the real model against the fake
/// transport, so each PNG shows a state the decision flow can produce.
@MainActor
enum ConfirmCardSnapshotChecks {
    static let size = NSSize(width: 480, height: 560)
    static let now = Date(timeIntervalSince1970: 1_792_000_000)

    static func ticket(
        channel: String = "message.send", riskClass: String = "R2",
        label: String = "Send via Messages to Sam Rivera?",
        recipient: ConfirmRecipient? = ConfirmRecipient(
            name: "Sam Rivera", address: "+1 555-0100", addressKind: "phone",
            firstContact: false
        ),
        text: String? = "Running 10 min late — start without me? This is the full message body, shown in full no matter how long it gets, scrolling instead of truncating so the owner always sees exactly what will be sent.",
        diff: ConfirmDiff? = nil,
        undo: UndoInfo = UndoInfo(kind: "none", label: "Sending cannot be undone"),
        expiresIn: TimeInterval? = 83
    ) -> NeedsConfirm {
        NeedsConfirm(
            ticketId: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            riskClass: riskClass, channel: channel, label: label,
            recipient: recipient, text: text, diff: diff,
            undo: undo,
            expiresAt: expiresIn.map { ISO8601DateFormatter().string(from: now.addingTimeInterval($0)) },
            approve: ConfirmEndpoint(path: "/v1/fabric/approvals/{ticket_id}/approve",
                                     needsReverify: true),
            deny: ConfirmEndpoint(path: "/v1/fabric/approvals/{ticket_id}/deny")
        )
    }

    static func model(_ ticket: NeedsConfirm) -> ConfirmCardModel {
        let model = ConfirmCardModel(ticket: ticket, client: FakeConfirmClient())
        model.setNow(now)
        return model
    }

    static func run(_ check: CheckFn) async {
        let pending: [(String, ConfirmCardModel)] = [
            ("card-send", model(ticket())),
            ("card-first-contact", model(ticket(
                label: "Send via Messages to +1 555-0199?",
                recipient: ConfirmRecipient(
                    name: "", address: "+1 555-0199", addressKind: "phone",
                    firstContact: true
                )
            ))),
            ("card-file-diff", model(ticket(
                channel: "file.write", riskClass: "R2",
                label: "Write shopping.md in Documents?",
                recipient: nil, text: nil,
                diff: ConfirmDiff(
                    kind: "unified_diff",
                    body: """
                        --- a/shopping.md
                        +++ b/shopping.md
                        @@ -1,3 +1,4 @@
                         - milk
                         + oat milk
                         + eggs
                           bread
                        """,
                    truncated: false
                ),
                undo: UndoInfo(kind: "file_snapshot", label: "Can be undone (pre-write snapshot)")
            ))),
            ("card-sql", model(ticket(
                channel: "sql.dml", riskClass: "R3",
                label: "Run 1 statement on shop.db?",
                recipient: nil, text: nil,
                diff: ConfirmDiff(
                    kind: "sql_preview",
                    body: "UPDATE items SET qty = qty - 1 WHERE sku = 'oat-milk'; -- 1 row",
                    truncated: true
                ),
                undo: UndoInfo(kind: "none", label: "Database writes cannot be undone")
            ))),
            ("card-minimal", model(NeedsConfirm(
                ticketId: "t-min", riskClass: "R2", channel: "message.send",
                label: "Send the draft?",
                undo: UndoInfo(kind: "none"),
                expiresAt: ISO8601DateFormatter().string(from: now.addingTimeInterval(83))
            ))),
            ("card-expired", model(ticket(expiresIn: -5))),
        ]
        // Result states, driven through the real model.
        let approvedModel = model(ticket())
        await approvedModel.approve()
        let deniedModel = model(ticket())
        await deniedModel.deny()
        let failedModel = ConfirmCardModel(
            ticket: ticket(),
            client: FakeConfirmClient(approvePlan: [
                .failure(.staleTicket(message: "That request is gone — it may have expired.")),
            ])
        )
        failedModel.setNow(now)
        await failedModel.approve()
        let resulted: [(String, ConfirmCardModel)] = [
            ("card-approved", approvedModel),
            ("card-denied", deniedModel),
            ("card-failed", failedModel),
        ]
        for scheme: ColorScheme in [.light, .dark] {
            let tag = scheme == .dark ? "dark" : "light"
            for (name, cardModel) in pending + resulted {
                let url = SnapshotRender.render(name: "\(name)-\(tag)", size: size, scheme: scheme) {
                    ConfirmCardView(model: cardModel)
                }
                if url == nil {
                    check("snap-\(name)-\(tag)", false, "render failed")
                } else {
                    SnapshotRender.checkPNG(name, scheme: tag, expectedSize: size, check: check)
                }
            }
        }
    }
}
