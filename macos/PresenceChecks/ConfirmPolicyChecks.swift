import Foundation
import PresenceCore

/// Confirm policy: the spoken-never rule, the rich gate, expiry math,
/// and human names. All pure.
enum ConfirmPolicyChecks {
    static func ticket(channel: String, firstContact: Bool = false,
                       rich: Bool = true) -> NeedsConfirm {
        NeedsConfirm(
            ticketId: "t1", riskClass: "R2", channel: channel, label: "Do it",
            recipient: rich
                ? ConfirmRecipient(name: "Sam", address: "x", firstContact: firstContact)
                : nil,
            text: rich ? "hello" : nil
        )
    }

    static func run(_ check: CheckFn) {
        for channel in ["message.send", "mail.send", "call.place", "file.delete",
                        "sql.dml", "sql.ddl", "credential.use", "money"] {
            check(
                "spoken-never-\(channel)",
                ConfirmPolicy.spokenMayApprove(ticket(channel: channel)) == false
            )
        }
        // First contact vetoes speech on ANY channel, even low-risk ones.
        check(
            "spoken-never-first-contact",
            ConfirmPolicy.spokenMayApprove(ticket(channel: "file.write", firstContact: true)) == false
        )
        // Low-risk, known-contact tickets remain voice-approvable.
        check(
            "spoken-allows-low-risk",
            ConfirmPolicy.spokenMayApprove(ticket(channel: "file.write"))
                && ConfirmPolicy.spokenMayApprove(ticket(channel: "other"))
        )
        check(
            "physical-needs-rich",
            ConfirmPolicy.physicalMayApprove(ticket(channel: "file.write", rich: true))
                && !ConfirmPolicy.physicalMayApprove(ticket(channel: "file.write", rich: false))
        )

        let now = Date(timeIntervalSince1970: 1_792_000_000)
        let fmt = ISO8601DateFormatter()
        let future = fmt.string(from: now.addingTimeInterval(83))
        let past = fmt.string(from: now.addingTimeInterval(-5))
        check("expiry-future", !ConfirmPolicy.isExpired(expiresAt: future, now: now))
        check("expiry-past", ConfirmPolicy.isExpired(expiresAt: past, now: now))
        check("expiry-missing-is-not-expired", !ConfirmPolicy.isExpired(expiresAt: nil, now: now))
        check("expiry-malformed-is-not-expired", !ConfirmPolicy.isExpired(expiresAt: "soon", now: now))
        check(
            "expiry-text",
            ConfirmPolicy.expiryText(expiresAt: future, now: now) == "Expires in 1:23"
                && ConfirmPolicy.expiryText(expiresAt: past, now: now) == "Expired"
                && ConfirmPolicy.expiryText(expiresAt: nil, now: now) == nil
                && ConfirmPolicy.expiryText(
                    expiresAt: fmt.string(from: now.addingTimeInterval(9)), now: now
                ) == "Expires in 9s"
        )
        check(
            "channel-names",
            ConfirmPolicy.channelName("message.send") == "Message"
                && ConfirmPolicy.channelName("sql.ddl") == "Database schema change"
                && ConfirmPolicy.channelName("future.thing") == "future.thing"
                && ConfirmPolicy.channelName("") == "Action"
        )
        check(
            "risk-names",
            ConfirmPolicy.riskName("R3") == "R3 · high"
                && ConfirmPolicy.riskName("forbidden") == "Forbidden"
                && ConfirmPolicy.riskName("") == "Unrated"
        )
    }
}
