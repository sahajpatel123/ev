import Foundation

/// Rewind timeline data: what Evie did, from the EXISTING read APIs
/// (`GET /v1/runtime/actions`, `GET /v1/fabric/undo`) plus restore through
/// `POST /v1/fabric/undo/restore`. Everything else is read-only. The
/// unified receipt feed (`receipt_feed`) is CLI 1's; until it ships, rows
/// come from these two sources and the checks' fixtures prove the shapes.
public struct TimelineAction: Sendable, Equatable {
    public var id: String
    public var at: Date?
    public var title: String
    public var actionType: String
    public var status: String
    public var target: String
    public var verified: Bool?
    public var ok: Bool?
    public var error: String?
    public var rolledBack: Bool
    public init(
        id: String, at: Date? = nil, title: String = "", actionType: String = "",
        status: String = "", target: String = "", verified: Bool? = nil,
        ok: Bool? = nil, error: String? = nil, rolledBack: Bool = false
    ) {
        self.id = id; self.at = at; self.title = title; self.actionType = actionType
        self.status = status; self.target = target; self.verified = verified
        self.ok = ok; self.error = error; self.rolledBack = rolledBack
    }
}

public struct TimelineUndoEntry: Sendable, Equatable {
    public var id: String
    public var rel: String
    public var kind: String
    public var at: Date?
    public init(id: String, rel: String = "", kind: String = "", at: Date? = nil) {
        self.id = id; self.rel = rel; self.kind = kind; self.at = at
    }
}

public enum TimelineError: Error, Sendable, Equatable {
    case authFailed(message: String)
    case requestFailed(message: String)

    public var message: String {
        switch self {
        case .authFailed(let m): return m
        case .requestFailed(let m): return m
        }
    }
}

public protocol TimelineClient: Sendable {
    func fetchActions(limit: Int) async throws -> [TimelineAction]
    func fetchUndoEntries() async throws -> [TimelineUndoEntry]
    /// Human result line ("Restored <rel>") or a thrown honest error.
    func restoreUndo(id: String) async throws -> String
}

// MARK: - Pure parsers (checked headless against backend-shaped fixtures)

public enum TimelineParser {
    static func isoDate(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let fmt = ISO8601DateFormatter()
        fmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fmt.date(from: raw) { return date }
        return ISO8601DateFormatter().date(from: raw)
    }

    /// `GET /v1/runtime/actions` → `[TimelineAction]`. The list is a bare
    /// JSON array of approval rows; unknown rows are skipped, never fatal.
    public static func parseActions(_ data: Data) -> [TimelineAction] {
        guard let array = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            return []
        }
        return array.compactMap(parseActionRow)
    }

    static func parseActionRow(_ row: [String: Any]) -> TimelineAction? {
        guard let id = row["id"] as? String, !id.isEmpty else { return nil }
        let payload = row["payload"] as? [String: Any] ?? [:]
        let pol = payload["_pol"] as? [String: Any] ?? [:]
        let result = row["result"] as? [String: Any]
        return TimelineAction(
            id: id,
            at: isoDate(row["created_at"] as? String),
            title: row["title"] as? String ?? "",
            actionType: row["action_type"] as? String ?? "",
            status: row["status"] as? String ?? "",
            target: pol["target"] as? String ?? "",
            verified: result?["verified"] as? Bool,
            ok: result?["ok"] as? Bool,
            error: row["error"] as? String,
            rolledBack: row["rolled_back_at"] as? String != nil
        )
    }

    /// `GET /v1/fabric/undo` → entries. `{ok, entries, count}` envelope.
    public static func parseUndoList(_ data: Data) -> [TimelineUndoEntry] {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let entries = object["entries"] as? [[String: Any]]
        else {
            return []
        }
        return entries.compactMap { entry in
            guard let id = entry["id"] as? String, !id.isEmpty else { return nil }
            var at: Date?
            if let epoch = entry["at"] as? Double {
                at = Date(timeIntervalSince1970: epoch)
            } else if let epoch = entry["at"] as? Int {
                at = Date(timeIntervalSince1970: Double(epoch))
            }
            return TimelineUndoEntry(
                id: id,
                rel: entry["rel"] as? String ?? "",
                kind: entry["kind"] as? String ?? "",
                at: at
            )
        }
    }

    /// `POST /v1/fabric/undo/restore` → human result line or honest error.
    public static func parseRestore(statusCode: Int, body: Data, rel: String) -> Result<String, TimelineError> {
        func detail(_ fallback: String) -> String {
            if let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
               let detail = object["detail"] as? String, !detail.isEmpty {
                return detail
            }
            if let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
               let error = object["error"] as? String, !error.isEmpty {
                return error
            }
            return fallback
        }
        switch statusCode {
        case 200, 201:
            if let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
               object["ok"] as? Bool == false {
                return .failure(.requestFailed(message: detail("Restore failed.")))
            }
            return .success(rel.isEmpty ? "Restored." : "Restored \(rel).")
        case 401:
            return .failure(.authFailed(message: detail("Backend auth failed.")))
        case 404:
            return .failure(.requestFailed(message: detail("That restore point is gone.")))
        case 409, 422:
            return .failure(.requestFailed(message: detail("Restore refused: nothing was changed.")))
        default:
            return .failure(.requestFailed(message: detail("Restore failed (status \(statusCode)).")))
        }
    }
}

// MARK: - URLSession implementation (thin runner over the pure parsers)

public final class APITimelineClient: TimelineClient, Sendable {
    private let baseURL: URL
    private let token: String
    private let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    public func fetchActions(limit: Int) async throws -> [TimelineAction] {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("v1/runtime/actions"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [URLQueryItem(name: "limit", value: String(max(1, min(limit, 200))))]
        let (data, response) = try await get(components?.url ?? baseURL)
        try checkAuth(response)
        return TimelineParser.parseActions(data)
    }

    public func fetchUndoEntries() async throws -> [TimelineUndoEntry] {
        let (data, response) = try await get(baseURL.appendingPathComponent("v1/fabric/undo"))
        try checkAuth(response)
        return TimelineParser.parseUndoList(data)
    }

    public func restoreUndo(id: String) async throws -> String {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/fabric/undo/restore"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = (try? JSONSerialization.data(withJSONObject: ["id": id])) ?? Data()
        request.timeoutInterval = 30
        let (data, response) = try await send(request)
        guard let http = response as? HTTPURLResponse else {
            throw TimelineError.requestFailed(message: "Non-HTTP response restoring.")
        }
        switch TimelineParser.parseRestore(statusCode: http.statusCode, body: data, rel: "") {
        case .success(let line): return line
        case .failure(let error): throw error
        }
    }

    private func get(_ url: URL) async throws -> (Data, URLResponse) {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 30
        return try await send(request)
    }

    private func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        do {
            return try await session.data(for: request)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw TimelineError.requestFailed(message: "Network error: \(error.localizedDescription)")
        }
    }

    private func checkAuth(_ response: URLResponse) throws {
        if let http = response as? HTTPURLResponse, http.statusCode == 401 {
            throw TimelineError.authFailed(message: "Backend auth failed — the app's credential is wrong.")
        }
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw TimelineError.requestFailed(message: "The backend returned status \(http.statusCode).")
        }
    }
}
