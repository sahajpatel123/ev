import Foundation
import PresenceCore

/// Timeline parsers against backend-shaped fixtures (approval rows,
/// undo-list envelope, restore receipts).
enum TimelineParserChecks {
    static let actionsJSON = """
    [
      {"id": "a1", "action_type": "comms_act", "title": "Text Sam",
       "payload": {"_pol": {"target": "Sam (Messages)", "risk_class": "R2"}},
       "status": "executed", "requires_approval": true,
       "requested_by": "owner", "device_id": null, "session_id": null,
       "approved_at": "2026-10-26T10:00:00Z", "approved_by": "owner",
       "denied_at": null, "denied_reason": null,
       "executed_at": "2026-10-26T10:00:05Z",
       "result": {"ok": true, "verified": true}, "error": null,
       "rolled_back_at": null, "rolled_back_reason": null,
       "created_at": "2026-10-26T09:59:00Z", "updated_at": "2026-10-26T10:00:05Z"},
      {"id": "a2", "action_type": "sql_query", "title": "",
       "payload": {}, "status": "failed", "requires_approval": false,
       "requested_by": "owner", "device_id": null, "session_id": null,
       "approved_at": null, "approved_by": null, "denied_at": null,
       "denied_reason": null, "executed_at": null, "result": null,
       "error": "no such table", "rolled_back_at": null,
       "rolled_back_reason": null,
       "created_at": "2026-10-25T08:00:00.123456Z", "updated_at": "2026-10-25T08:00:01Z"},
      {"id": "a3", "action_type": "files_op", "title": "Write notes",
       "payload": {"_pol": {"target": "~/Documents/notes.md"}},
       "status": "executed", "requires_approval": false,
       "requested_by": "owner", "device_id": null, "session_id": null,
       "approved_at": null, "approved_by": null, "denied_at": null,
       "denied_reason": null, "executed_at": "2026-10-26T11:00:00Z",
       "result": {"ok": true, "verified": false}, "error": null,
       "rolled_back_at": "2026-10-26T11:05:00Z", "rolled_back_reason": "owner",
       "created_at": "2026-10-26T11:00:00Z", "updated_at": "2026-10-26T11:05:00Z"},
      {"no_id": true}
    ]
    """

    static let undoJSON = """
    {"ok": true, "count": 2, "entries": [
      {"id": "/Users/o/Documents::notes.md.2026.info.json", "root": "/Users/o/Documents",
       "rel": "notes.md", "kind": "snapshot", "at": 1792000000.5},
      {"id": "/Users/o/Desktop::old.txt.info.json", "root": "/Users/o/Desktop",
       "rel": "old.txt", "kind": "trash", "at": 1791000000},
      {"rel": "orphan"}
    ]}
    """

    static func run(_ check: CheckFn) {
        let actions = TimelineParser.parseActions(Data(actionsJSON.utf8))
        check("timeline-parse-actions-count", actions.count == 3, "got \(actions.count)")
        if actions.count == 3 {
            check(
                "timeline-parse-executed",
                actions[0].target == "Sam (Messages)" && actions[0].verified == true
                    && actions[0].ok == true && !actions[0].rolledBack
                    && actions[0].at != nil,
                "\(actions[0])"
            )
            check(
                "timeline-parse-failed",
                actions[1].error == "no such table" && actions[1].verified == nil
                    && actions[1].title.isEmpty && actions[1].at != nil,
                "\(actions[1])"
            )
            check(
                "timeline-parse-rolled-back",
                actions[2].rolledBack && actions[2].verified == false
                    && actions[2].target == "~/Documents/notes.md",
                "\(actions[2])"
            )
        }
        check("timeline-parse-actions-bad", TimelineParser.parseActions(Data("{}".utf8)).isEmpty)

        let entries = TimelineParser.parseUndoList(Data(undoJSON.utf8))
        check("timeline-parse-undo-count", entries.count == 2, "got \(entries.count)")
        if entries.count == 2 {
            check(
                "timeline-parse-undo",
                entries[0].rel == "notes.md" && entries[0].kind == "snapshot"
                    && entries[0].at == Date(timeIntervalSince1970: 1792000000.5)
                    && entries[1].kind == "trash",
                "\(entries)"
            )
        }
        check("timeline-parse-undo-bad", TimelineParser.parseUndoList(Data("[]".utf8)).isEmpty)

        func restore(_ code: Int, _ json: String) -> Result<String, TimelineError> {
            TimelineParser.parseRestore(statusCode: code, body: Data(json.utf8), rel: "notes.md")
        }
        if case .success(let line) = restore(200, #"{"ok": true, "restored_at": 1}"#) {
            check("timeline-restore-ok", line == "Restored notes.md.", line)
        } else {
            check("timeline-restore-ok", false)
        }
        if case .failure(let e) = restore(200, #"{"ok": false, "error": "locked"}"#) {
            check("timeline-restore-ok-false", e.message == "locked", e.message)
        } else {
            check("timeline-restore-ok-false", false)
        }
        if case .failure(let e) = restore(404, #"{"detail": "gone"}"#) {
            check("timeline-restore-404", e.message == "gone", e.message)
        } else {
            check("timeline-restore-404", false)
        }
        if case .failure(let e) = restore(409, #"{}"#) {
            check("timeline-restore-409", e.message.contains("nothing was changed"), e.message)
        } else {
            check("timeline-restore-409", false)
        }
        if case .failure(.authFailed) = restore(401, #"{}"#) {
            check("timeline-restore-401", true)
        } else {
            check("timeline-restore-401", false)
        }
    }
}
