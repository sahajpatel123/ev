import Foundation
import PresenceCore

/// Contract-conformance decoding: every JSON body below is taken from
/// `docs/contracts/PRESENCE_EVENTS.md` §3. Unknown types/keys must decode,
/// never throw (additive rule).
enum PresenceEventChecks {
    static func decode(_ json: String) throws -> PresenceEvent {
        try JSONDecoder().decode(PresenceEvent.self, from: Data(json.utf8))
    }

    static func frame(type: String, data: String, id: String = "pe-test-1",
                      turnId: String? = "ft-a1b2") -> String {
        let turn = turnId.map { "\"turn_id\": \"\($0)\"," } ?? "\"turn_id\": null,"
        return """
        {"v": "evie-presence/1", "id": "\(id)", "seq": 41207,
         "ts": "2026-10-26T19:02:11.410Z", \(turn)
         "type": "\(type)", "data": \(data)}
        """
    }

    static func run(_ check: CheckFn) {
        do {
            let hello = try decode(frame(
                type: "stream.hello",
                data: """
                {"contract": "evie-presence/1", "server_time": "2026-10-26T19:02:11.410Z",
                 "cursor": "pe-01", "resumed": true, "gap": false, "restarted": false,
                 "capabilities": ["rich_confirm", "receipt_feed", "poll_fallback"]}
                """,
                turnId: nil
            ))
            if case .streamHello(let h) = hello.payload {
                check("event-hello", h.resumed && h.capabilities.count == 3 && hello.turnId == nil)
            } else {
                check("event-hello", false, "wrong payload \(hello.payload)")
            }
        } catch {
            check("event-hello", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "turn.started",
                data: #"{"source": "gesture", "utterance_preview": "", "label": "Listening…"}"#
            ))
            if case .turnStarted(let s) = e.payload {
                check("event-turn-started", s.source == "gesture" && e.turnId == "ft-a1b2")
            } else {
                check("event-turn-started", false, "wrong payload")
            }
        } catch {
            check("event-turn-started", false, "\(error)")
        }

        do {
            let e = try decode(frame(type: "turn.listening", data: #"{"level": 0.42, "degraded": false}"#))
            if case .turnListening(let l) = e.payload {
                check("event-listening", l.level == 0.42 && !l.degraded)
            } else {
                check("event-listening", false, "wrong payload")
            }
        } catch {
            check("event-listening", false, "\(error)")
        }

        do {
            let p = try decode(frame(
                type: "turn.transcript.partial",
                data: #"{"text": "text Sam that I'll be", "sequence": 7, "stable": false, "confidence": 0.61}"#
            ))
            let f = try decode(frame(
                type: "turn.transcript.final",
                data: #"{"text": "Text Sam.", "confidence": 0.93, "provider": "live-voice"}"#
            ))
            var ok = false
            if case .transcriptPartial(let part) = p.payload,
               case .transcriptFinal(let fin) = f.payload {
                ok = part.sequence == 7 && !part.stable && fin.provider == "live-voice"
            }
            check("event-transcripts", ok)
        } catch {
            check("event-transcripts", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "turn.thinking",
                data: #"{"label": "Thinking…", "elapsed_ms": 850}"#
            ))
            if case .turnThinking(let t) = e.payload {
                check("event-thinking", t.elapsedMs == 850)
            } else {
                check("event-thinking", false, "wrong payload")
            }
        } catch {
            check("event-thinking", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "plan.updated",
                data: """
                {"label": "Texting Sam", "steps": [
                  {"id": "s1", "label": "Find Sam's number", "status": "done"},
                  {"id": "s2", "label": "Confirm with you", "status": "active"},
                  {"id": "s3", "label": "Send", "status": "queued"},
                  {"id": "s4", "label": "Future kind", "status": "mystery"}]}
                """
            ))
            if case .planUpdated(let p) = e.payload {
                check(
                    "event-plan",
                    p.steps.count == 4 && p.steps[1].status == .active
                        && p.steps[3].status == .other("mystery"),
                    "\(p.steps)"
                )
            } else {
                check("event-plan", false, "wrong payload")
            }
        } catch {
            check("event-plan", false, "\(error)")
        }

        do {
            let s = try decode(frame(
                type: "tool.started",
                data: #"{"call_id": "c-9f2", "step_id": "s2", "name": "comms_act", "label": "Drafting message", "target": "Sam (Messages)"}"#
            ))
            let f = try decode(frame(
                type: "tool.finished",
                data: """
                {"call_id": "c-9f2", "step_id": "s2", "name": "comms_act",
                 "label": "Draft ready", "ok": true, "verified": true,
                 "evidence": {"draft_id": "d-11", "tries": 2, "cached": true},
                 "error": null, "receipt_id": "rc-77", "undo": {"kind": "none"}}
                """
            ))
            var ok = false
            if case .toolStarted(let started) = s.payload,
               case .toolFinished(let fin) = f.payload {
                ok = started.target == "Sam (Messages)"
                    && fin.verified && fin.receiptId == "rc-77"
                    && fin.evidence["draft_id"] == .string("d-11")
                    && fin.evidence["tries"] == .number(2)
                    && fin.evidence["cached"] == .bool(true)
                    && !fin.undo.isUndoable
            }
            check("event-tools", ok)
        } catch {
            check("event-tools", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "ambient.looking_at",
                data: #"{"app": "Mail", "window": "Inbox", "bundle_id": "com.apple.mail"}"#
            ))
            if case .lookingAt(let l) = e.payload {
                check("event-looking-at", l.app == "Mail" && !l.isEmpty)
            } else {
                check("event-looking-at", false, "wrong payload")
            }
        } catch {
            check("event-looking-at", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "turn.needs_confirm",
                data: """
                {"ticket_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "risk_class": "R3",
                 "channel": "message.send", "label": "Send via Messages to Sam?",
                 "recipient": {"name": "Sam Rivera", "address": "+1 555-0100",
                              "address_kind": "phone", "first_contact": false},
                 "text": "Running 10 min late.",
                 "diff": null, "attachments": [],
                 "undo": {"kind": "none", "label": "Sending cannot be undone"},
                 "expires_at": "2026-10-26T19:04:11Z",
                 "approve": {"method": "POST", "path": "/v1/fabric/approvals/X/approve",
                             "needs_reverify": true},
                 "deny": {"method": "POST", "path": "/v1/fabric/approvals/X/deny"}}
                """
            ))
            if case .needsConfirm(let c) = e.payload {
                check(
                    "event-needs-confirm-rich",
                    c.isRich && c.recipient?.firstContact == false
                        && c.approve?.needsReverify == true,
                    "rich=\(c.isRich)"
                )
            } else {
                check("event-needs-confirm-rich", false, "wrong payload")
            }
        } catch {
            check("event-needs-confirm-rich", false, "\(error)")
        }

        // Pre-rich_confirm server: label-only ticket, approve stays disabled.
        do {
            let e = try decode(frame(
                type: "turn.needs_confirm",
                data: """
                {"ticket_id": "abc", "risk_class": "R2", "channel": "message.send",
                 "label": "Send?", "attachments": [], "undo": {"kind": "none"},
                 "expires_at": "2026-10-26T19:04:11Z"}
                """
            ))
            if case .needsConfirm(let c) = e.payload {
                check(
                    "event-needs-confirm-minimal",
                    !c.isRich && c.approve == nil && c.text == nil,
                    "rich=\(c.isRich)"
                )
            } else {
                check("event-needs-confirm-minimal", false, "wrong payload")
            }
        } catch {
            check("event-needs-confirm-minimal", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "turn.progress",
                data: #"{"text": "Found Sam.", "spoken": true, "sequence": 3}"#
            ))
            if case .turnProgress(let p) = e.payload {
                check("event-progress", p.spoken && p.sequence == 3)
            } else {
                check("event-progress", false, "wrong payload")
            }
        } catch {
            check("event-progress", false, "\(error)")
        }

        do {
            let c = try decode(frame(
                type: "turn.completed",
                data: #"{"summary": "Sent to Sam.", "receipt_ids": ["rc-77"], "verified": true, "label": "Done"}"#
            ))
            let f = try decode(frame(
                type: "turn.failed",
                data: """
                {"reason": "No number on file.", "category": "missing_data",
                 "failed_step_id": "s1", "recoverable": true,
                 "next_step": "Add the number.", "receipt_ids": []}
                """
            ))
            var ok = false
            if case .turnCompleted(let done) = c.payload,
               case .turnFailed(let fail) = f.payload {
                ok = done.verified && fail.recoverable && fail.failedStepId == "s1"
            }
            check("event-terminal", ok)
        } catch {
            check("event-terminal", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "background.update",
                data: #"{"kind": "routine.fired", "label": "Briefing ready.", "ref": {"type": "briefing", "id": "b-42"}}"#
            ))
            if case .backgroundUpdate(let b) = e.payload {
                check("event-background", b.ref?.id == "b-42")
            } else {
                check("event-background", false, "wrong payload")
            }
        } catch {
            check("event-background", false, "\(error)")
        }

        do {
            let e = try decode(frame(
                type: "panic.state",
                data: #"{"panicked": true, "at": "2026-10-26T19:05:00Z", "reason": "Hotkey."}"#
            ))
            if case .panicState(let p) = e.payload {
                check("event-panic", p.panicked && p.reason == "Hotkey.")
            } else {
                check("event-panic", false, "wrong payload")
            }
        } catch {
            check("event-panic", false, "\(error)")
        }

        // Additive rule: unknown type + unknown keys decode, never throw.
        do {
            let e = try decode(frame(
                type: "turn.hologram",
                data: #"{"future": [1, 2, {"deep": true}], "label": "hi"}"#
            ))
            if case .unknown(let t) = e.payload {
                check("event-unknown-tolerated", t == "turn.hologram")
            } else {
                check("event-unknown-tolerated", false, "wrong payload")
            }
            let e2 = try decode(frame(
                type: "turn.thinking",
                data: #"{"label": "x", "elapsed_ms": 1, "new_key": {"n": 1}}"#
            ))
            if case .turnThinking(let t) = e2.payload {
                check("event-unknown-keys-ignored", t.label == "x")
            } else {
                check("event-unknown-keys-ignored", false, "wrong payload")
            }
        } catch {
            check("event-unknown-tolerated", false, "\(error)")
            check("event-unknown-keys-ignored", false, "\(error)")
        }

        // Missing id is a hard error (dedupe needs it); missing data defaults.
        do {
            _ = try decode("""
            {"v": "evie-presence/1", "type": "turn.thinking", "data": {"label": "x"}}
            """)
            check("event-missing-id-rejected", false, "decoded without id")
        } catch {
            check("event-missing-id-rejected", true)
        }
    }
}
