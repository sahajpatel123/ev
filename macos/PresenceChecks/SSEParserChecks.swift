import Foundation
import PresenceCore

/// SSE frame parser, stream decode mapping, and backoff. All pure — the
/// socket loop itself is never opened in checks (P3: no live :8000 calls).
enum SSEParserChecks {
    static func run(_ check: CheckFn) {
        // One complete frame across feed() calls.
        do {
            var parser = SSEFrameParser()
            var outputs: [SSEFrameParser.Output] = []
            for line in ["id: pe-1", "event: turn.thinking", #"data: {"a": 1}"#, ""] {
                if let out = parser.feed(line: line) { outputs.append(out) }
            }
            if case .frame(let frame) = outputs.first, outputs.count == 1 {
                check(
                    "sse-frame",
                    frame.id == "pe-1" && frame.event == "turn.thinking"
                        && frame.data == #"{"a": 1}"#,
                    "\(frame)"
                )
            } else {
                check("sse-frame", false, "\(outputs)")
            }
        }
        // Heartbeats and unknown fields never complete a frame.
        do {
            var parser = SSEFrameParser()
            let hb = parser.feed(line: ":heartbeat")
            let retry = parser.feed(line: "retry: 3000")
            check("sse-heartbeat", hb == .heartbeat && retry == nil)
        }
        // Multi-line data joins with \n; id persists per frame only.
        do {
            var parser = SSEFrameParser()
            var frames: [SSEFrameParser.Frame] = []
            for line in ["id: a", "data: {", "data: }", "",
                         "data: second", ""] {
                if let out = parser.feed(line: line), case .frame(let f) = out {
                    frames.append(f)
                }
            }
            check(
                "sse-multiline",
                frames.count == 2 && frames[0].data == "{\n}"
                    && frames[0].id == "a" && frames[1].id == nil,
                "\(frames)"
            )
        }
        // Event with no data is malformed: counted, skipped, stream lives.
        do {
            var parser = SSEFrameParser()
            var outputs: [SSEFrameParser.Output] = []
            for line in ["event: turn.thinking", "",
                         "event: turn.thinking", #"data: {"a": 1}"#, ""] {
                if let out = parser.feed(line: line) { outputs.append(out) }
            }
            check(
                "sse-malformed-skipped",
                outputs.count == 1 && parser.malformedCount == 1,
                "\(outputs) malformed=\(parser.malformedCount)"
            )
        }
        // Unterminated tail frame flushes at finish().
        do {
            var parser = SSEFrameParser()
            _ = parser.feed(line: "id: tail")
            _ = parser.feed(line: "data: {}")
            if let out = parser.finish(), case .frame(let f) = out {
                check("sse-finish-flush", f.id == "tail")
            } else {
                check("sse-finish-flush", false)
            }
        }
        // decode(): valid contract JSON, unknown type, garbage.
        do {
            let valid = SSEFrameParser.Frame(
                id: "pe-1", event: "turn.thinking",
                data: """
                {"v": "evie-presence/1", "id": "pe-1", "seq": 1,
                 "ts": "2026-10-26T19:02:11Z", "turn_id": "ft-1",
                 "type": "turn.thinking",
                 "data": {"label": "Thinking…", "elapsed_ms": 1}}
                """
            )
            let event = SSEPresenceTransport.decode(frame: valid)
            if case .turnThinking = event?.payload {
                check("sse-decode-valid", event?.id == "pe-1")
            } else {
                check("sse-decode-valid", false, "\(String(describing: event))")
            }
            let future = SSEFrameParser.Frame(
                id: "pe-2", event: "turn.hologram",
                data: """
                {"v": "evie-presence/1", "id": "pe-2", "seq": 2,
                 "ts": "", "turn_id": null, "type": "turn.hologram", "data": {}}
                """
            )
            let unknown = SSEPresenceTransport.decode(frame: future)
            if case .unknown(let t) = unknown?.payload {
                check("sse-decode-unknown", t == "turn.hologram")
            } else {
                check("sse-decode-unknown", false)
            }
            let garbage = SSEFrameParser.Frame(id: "pe-3", event: "x", data: "not json")
            check("sse-decode-garbage", SSEPresenceTransport.decode(frame: garbage) == nil)
            let noEnvelope = SSEFrameParser.Frame(
                id: "pe-4", event: "x", data: #"{"type": "turn.thinking"}"#
            )
            check("sse-decode-no-id", SSEPresenceTransport.decode(frame: noEnvelope) == nil)
        }
        // Backoff: 1s doubling to a 30s ceiling.
        check(
            "sse-backoff",
            PresenceBackoff.delay(attempt: 0) == 1
                && PresenceBackoff.delay(attempt: 1) == 2
                && PresenceBackoff.delay(attempt: 4) == 16
                && PresenceBackoff.delay(attempt: 5) == 30
                && PresenceBackoff.delay(attempt: 99) == 30
        )
        // Cursor round-trips through its own UserDefaults key (test key).
        do {
            let transport = SSEPresenceTransport(
                baseURL: URL(string: "http://127.0.0.1:9")!,
                token: "test", deviceId: "d",
                cursorKey: "EV_PRESENCE_CURSOR_TEST"
            )
            UserDefaults.standard.removeObject(forKey: "EV_PRESENCE_CURSOR_TEST")
            let before: String? = transport.cursor
            transport.cursor = "pe-99"
            let after: String? = transport.cursor
            transport.cursor = nil
            let cleared: String? = transport.cursor
            check(
                "sse-cursor",
                before == nil && after == "pe-99" && cleared == nil,
                "\(String(describing: before)) \(String(describing: after))"
            )
        }
    }
}
