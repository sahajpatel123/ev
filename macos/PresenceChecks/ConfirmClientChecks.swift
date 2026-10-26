import Foundation
import PresenceCore

/// Decision wire format: exact URLs/bodies and response mapping, all pure.
/// The thin URLSession runner is covered by source wiring checks, not live
/// calls (P3/P4: no network in checks).
enum ConfirmClientChecks {
    static let base = URL(string: "http://127.0.0.1:8000")!

    static func run(_ check: CheckFn) {
        // Ticket-supplied paths win, with {ticket_id} expanded.
        check(
            "confirm-url-ticket-path",
            ConfirmRequest.approveURL(
                base: base, path: "/v1/fabric/approvals/{ticket_id}/approve", ticketId: "abc"
            ).absoluteString == "http://127.0.0.1:8000/v1/fabric/approvals/abc/approve"
        )
        // Minimal tickets fall back to the fabric routes (same authority).
        check(
            "confirm-url-fabric-default",
            ConfirmRequest.approveURL(base: base, path: "", ticketId: "abc").absoluteString
                == "http://127.0.0.1:8000/v1/fabric/approvals/abc/approve"
                && ConfirmRequest.denyURL(base: base, path: "  ", ticketId: "abc").absoluteString
                == "http://127.0.0.1:8000/v1/fabric/approvals/abc/deny"
        )
        check(
            "confirm-url-reverify",
            ConfirmRequest.reverifyURL(base: base).absoluteString
                == "http://127.0.0.1:8000/v1/identity/reverification"
        )
        let body = try? JSONSerialization.jsonObject(with: ConfirmRequest.decisionBody(reason: "r"))
            as? [String: String]
        check("confirm-body-reason", body?["reason"] == "r")
        let rbody = try? JSONSerialization.jsonObject(with: ConfirmRequest.reverifyBody())
            as? [String: String]
        check("confirm-body-purpose", rbody?["purpose"] == "runtime.action")

        func parse(_ code: Int, _ json: String) -> Result<ConfirmDecision, ConfirmError> {
            ConfirmResponse.parseDecision(ticketId: "t", statusCode: code, body: Data(json.utf8))
        }
        if case .success(let d) = parse(200, #"{"status": "approved"}"#) {
            check("confirm-parse-200", d.status == "approved" && d.ticketId == "t")
        } else {
            check("confirm-parse-200", false)
        }
        if case .success(let d) = parse(201, #"{}"#) {
            check("confirm-parse-201-minimal", d.status == "decided")
        } else {
            check("confirm-parse-201-minimal", false)
        }
        if case .failure(.staleTicket(let m)) = parse(404, #"{"detail": "gone"}"#) {
            check("confirm-parse-404", m == "gone")
        } else {
            check("confirm-parse-404", false)
        }
        if case .failure(.staleTicket(let m)) = parse(404, #"{}"#) {
            check("confirm-parse-404-fallback", m.contains("expired"))
        } else {
            check("confirm-parse-404-fallback", false)
        }
        if case .failure(.alreadyDecided(let m)) = parse(409, #"{"detail": "decided"}"#) {
            check("confirm-parse-409", m == "decided")
        } else {
            check("confirm-parse-409", false)
        }
        if case .failure(.needsReverify) = parse(403, #"{"detail": "x"}"#) {
            check("confirm-parse-403", true)
        } else {
            check("confirm-parse-403", false)
        }
        if case .failure(.authFailed) = parse(401, #"{}"#) {
            check("confirm-parse-401", true)
        } else {
            check("confirm-parse-401", false)
        }
        if case .failure(.requestFailed(let m)) = parse(500, #"{}"#) {
            check("confirm-parse-500", m.contains("500"))
        } else {
            check("confirm-parse-500", false)
        }
        if case .success(let t) = ConfirmResponse.parseReverify(body: Data(
            #"{"token": "rv-1", "purpose": "runtime.action"}"#.utf8
        )) {
            check("confirm-parse-reverify", t == "rv-1")
        } else {
            check("confirm-parse-reverify", false)
        }
        if case .failure = ConfirmResponse.parseReverify(body: Data("{}".utf8)) {
            check("confirm-parse-reverify-empty", true)
        } else {
            check("confirm-parse-reverify-empty", false)
        }
    }
}
