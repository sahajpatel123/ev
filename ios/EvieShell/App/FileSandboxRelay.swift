import Foundation

/// iPhone → Home Station file-sandbox relay.
///
/// The iPhone NEVER touches the Mac filesystem directly. It sends the SAME
/// JSON the Mac app uses (`op` + `args` + `origin: "iphone"`) to the Home
/// Station backend (`/v1/file-sandbox/execute` or `/v1/file-sandbox/brain/*`),
/// which runs the shared `file_sandbox` jail and returns the same receipts.
/// Offline: the request is queued and replayed — never faked as success.
///
/// Brain lane: free text → `/v1/file-sandbox/brain/test` so Muse Spark 1.3
/// plans, dry-runs, then executes; the test + result receipts come back
/// together for the "tested, then ran" UI.
public enum FileSandboxRelay {
    public static let executePath = "/v1/file-sandbox/execute"
    public static let brainRunPath = "/v1/file-sandbox/brain/run"
    public static let brainTestPath = "/v1/file-sandbox/brain/test"

    public struct RelayRequest: Encodable {
        public var op: String
        public var args: [String: String]
        public var origin: String
        public var confirm: Bool
        public var dryRun: Bool

        public init(op: String, args: [String: String] = [:], confirm: Bool = false, dryRun: Bool = false) {
            self.op = op
            self.args = args
            self.origin = "iphone"
            self.confirm = confirm
            self.dryRun = dryRun
        }

        enum CodingKeys: String, CodingKey {
            case op, args, origin, confirm
            case dryRun = "dry_run"
        }
    }

    public struct BrainRequest: Encodable {
        public var text: String
        public var origin: String
        public var confirm: Bool
        public var dryRun: Bool

        public init(text: String, confirm: Bool = false, dryRun: Bool = false) {
            self.text = text
            self.origin = "iphone"
            self.confirm = confirm
            self.dryRun = dryRun
        }

        enum CodingKeys: String, CodingKey {
            case text, origin, confirm
            case dryRun = "dry_run"
        }
    }

    /// Encode an verb request for `POST {base}{executePath}`.
    /// Transport (URLSession + device token) stays with the caller so this
    /// file has no networking dependency and stays compilable in tests.
    public static func executeBody(op: String, args: [String: String], confirm: Bool, dryRun: Bool) -> Data? {
        try? JSONEncoder().encode(RelayRequest(op: op, args: args, confirm: confirm, dryRun: dryRun))
    }

    /// Encode a brain request for `POST {base}{brainTestPath}`.
    public static func brainTestBody(text: String) -> Data? {
        try? JSONEncoder().encode(BrainRequest(text: text))
    }

    /// Offline-queue envelope: what to persist when Home Station is
    /// unreachable. The queue replays verbatim; the server journal owns undo.
    public static func queuedEnvelope(op: String, args: [String: String]) -> [String: Any] {
        [
            "kind": "file_sandbox",
            "origin": "iphone",
            "op": op,
            "args": args,
            "queued_at": ISO8601DateFormatter().string(from: Date()),
            // Never mark queued work executed: the receipt arrives on replay.
            "executed": false,
        ]
    }
}
