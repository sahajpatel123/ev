import Foundation

/// Pure confirm-card policy: what the spoken word may approve, what counts
/// as rich enough to approve at all, expiry math, and channel names.
///
/// Authority rules (CLAUDE.md R7 + `fabric/policy.py` HIGH_RISK): a spoken
/// "yes" NEVER approves sends, deletes, SQL DML/DDL, money, credential, or
/// first-contact actions. Those need a PHYSICAL confirmation: Return/click
/// on this card (or the paired phone / typed reply, which travel other
/// paths). The server enforces the same rule; this is the client half.
public enum ConfirmPolicy {
    /// Channels a spoken "yes" must never approve.
    public static let spokenNeverChannels: Set<String> = [
        "message.send", "mail.send", "call.place",
        "file.delete", "sql.dml", "sql.ddl",
        "credential.use", "money",
    ]

    /// May the spoken word approve this ticket? False for HIGH_RISK
    /// channels and for ANY first-contact action, whatever the channel.
    public static func spokenMayApprove(_ ticket: NeedsConfirm) -> Bool {
        if ticket.recipient?.firstContact == true { return false }
        return !spokenNeverChannels.contains(ticket.channel)
    }

    /// May the physical approve control run? Only when the card rendered
    /// the full rich payload (contract §3). Label-only tickets cannot be
    /// approved from the app — the owner decides in the terminal instead.
    public static func physicalMayApprove(_ ticket: NeedsConfirm) -> Bool {
        ticket.isRich
    }

    /// True when `expiresAt` is a valid timestamp at or before `now`.
    /// A missing or malformed timestamp is NOT treated as expired — the
    /// server enforces TTL regardless, and the card must not invent one.
    public static func isExpired(expiresAt: String?, now: Date) -> Bool {
        guard let raw = expiresAt, let date = Self.date(from: raw) else {
            return false
        }
        return date <= now
    }

    /// "Expires in 1:23" / "Expired" / nil (unknown). Pure in `now`.
    public static func expiryText(expiresAt: String?, now: Date) -> String? {
        guard let raw = expiresAt, let date = Self.date(from: raw) else {
            return nil
        }
        let remaining = Int(date.timeIntervalSince(now))
        if remaining <= 0 { return "Expired" }
        if remaining < 60 { return "Expires in \(remaining)s" }
        return String(format: "Expires in %d:%02d", remaining / 60, remaining % 60)
    }

    public static func date(from raw: String) -> Date? {
        ISO8601DateFormatter().date(from: raw)
    }

    /// Human channel names. Unknown channels render verbatim, never blank.
    public static func channelName(_ channel: String) -> String {
        switch channel {
        case "message.send": return "Message"
        case "mail.send": return "Email"
        case "call.place": return "Phone call"
        case "file.write": return "File write"
        case "file.delete": return "File delete"
        case "sql.dml": return "Database change"
        case "sql.ddl": return "Database schema change"
        case "shell.exec": return "Shell command"
        case "publish": return "Publish"
        case "egress": return "Data export"
        case "credential.use": return "Credential use"
        default: return channel.isEmpty ? "Action" : channel
        }
    }

    /// Risk class chip text. Unknown classes render verbatim.
    public static func riskName(_ riskClass: String) -> String {
        switch riskClass {
        case "R0": return "R0 · negligible"
        case "R1": return "R1 · low"
        case "R2": return "R2 · medium"
        case "R3": return "R3 · high"
        case "R4": return "R4 · critical"
        case "forbidden": return "Forbidden"
        default: return riskClass.isEmpty ? "Unrated" : riskClass
        }
    }
}
