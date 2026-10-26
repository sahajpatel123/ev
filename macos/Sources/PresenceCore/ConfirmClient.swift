import Foundation

/// Decision transport for confirm tickets. The ONLY authority is the
/// existing approvals API (contract `approve`/`deny` endpoints, which
/// delegate to `POST /v1/runtime/actions/{id}/approve|deny`); this client
/// adds no second flow, no second ticket table, no invented auth.
public protocol ConfirmClient: Sendable {
    /// Approve. `reverify` carries an `X-EV-Reverify` token when the
    /// device-token path needs one; nil for master-key callers.
    func approve(ticketId: String, path: String, reason: String, reverify: String?) async throws -> ConfirmDecision
    func deny(ticketId: String, path: String, reason: String) async throws -> ConfirmDecision
    /// Mint a purpose-bound reverify token (`runtime.action`).
    func mintReverify() async throws -> String
}

public struct ConfirmDecision: Sendable, Equatable {
    public var ticketId: String
    public var status: String
    public var note: String?
    public init(ticketId: String, status: String, note: String? = nil) {
        self.ticketId = ticketId; self.status = status; self.note = note
    }
}

/// Typed decision failures. Every case maps to an honest card message.
public enum ConfirmError: Error, Sendable, Equatable {
    /// 404: the ticket is gone (expired, superseded, or never existed).
    case staleTicket(message: String)
    /// 409: someone already decided this ticket.
    case alreadyDecided(message: String)
    /// 403 reverification_required: the approve needs a fresh factor.
    case needsReverify(message: String)
    /// 401/other auth failures: the app's credential is wrong, not the ticket.
    case authFailed(message: String)
    /// Transport or unexpected status.
    case requestFailed(message: String)
    /// Client-side refusal: approve was never attempted (minimal ticket,
    /// expired card, spoken yes on HIGH_RISK). Not a server error.
    case refused(message: String)

    public var message: String {
        switch self {
        case .staleTicket(let m): return m
        case .alreadyDecided(let m): return m
        case .needsReverify(let m): return m
        case .authFailed(let m): return m
        case .requestFailed(let m): return m
        case .refused(let m): return m
        }
    }
}

// MARK: - Pure request builder (checked headless)

/// Exact bytes the app sends. The paths come from the ticket's own
/// `approve`/`deny` endpoints (contract §3), defaulting to the fabric
/// routes when the server sent a minimal ticket.
public enum ConfirmRequest {
    public static let approveReason = "Approved on the confirm card"
    public static let denyReason = "Denied on the confirm card"
    public static let reverifyPurpose = "runtime.action"

    public static func approveURL(base: URL, path: String, ticketId: String) -> URL {
        endpointURL(base: base, path: path, ticketId: ticketId, verb: "approve")
    }

    public static func denyURL(base: URL, path: String, ticketId: String) -> URL {
        endpointURL(base: base, path: path, ticketId: ticketId, verb: "deny")
    }

    private static func endpointURL(base: URL, path: String, ticketId: String, verb: String) -> URL {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            let expanded = trimmed.replacingOccurrences(of: "{ticket_id}", with: ticketId)
            if let url = URL(string: expanded, relativeTo: base) {
                return url.absoluteURL
            }
        }
        return base.appendingPathComponent("v1/fabric/approvals/\(ticketId)/\(verb)")
    }

    public static func reverifyURL(base: URL) -> URL {
        base.appendingPathComponent("v1/identity/reverification")
    }

    public static func decisionBody(reason: String) -> Data {
        (try? JSONSerialization.data(withJSONObject: ["reason": reason])) ?? Data()
    }

    public static func reverifyBody() -> Data {
        (try? JSONSerialization.data(withJSONObject: ["purpose": reverifyPurpose])) ?? Data()
    }
}

// MARK: - Pure response parser (checked headless)

public enum ConfirmResponse {
    /// Minimal approval-row projection: only what the card displays.
    public struct Row: Equatable {
        public var status: String
        public init(status: String) { self.status = status }
    }

    public static func parseDecision(ticketId: String, statusCode: Int, body: Data) -> Result<ConfirmDecision, ConfirmError> {
        switch statusCode {
        case 200, 201:
            let status = rowStatus(body: body) ?? "decided"
            return .success(ConfirmDecision(ticketId: ticketId, status: status))
        case 404:
            return .failure(.staleTicket(message: detail(body: body,
                fallback: "That request is gone — it may have expired.")))
        case 409:
            return .failure(.alreadyDecided(message: detail(body: body,
                fallback: "This request was already decided.")))
        case 401:
            return .failure(.authFailed(message: detail(body: body,
                fallback: "Backend auth failed — the app's credential is wrong.")))
        case 403:
            return .failure(.needsReverify(message: detail(body: body,
                fallback: "Fresh verification is required to approve.")))
        default:
            return .failure(.requestFailed(message: detail(body: body,
                fallback: "The backend returned status \(statusCode).")))
        }
    }

    public static func parseReverify(body: Data) -> Result<String, ConfirmError> {
        if let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
           let token = object["token"] as? String, !token.isEmpty {
            return .success(token)
        }
        if let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
           let token = object["reverify_token"] as? String, !token.isEmpty {
            return .success(token)
        }
        return .failure(.requestFailed(message: "The backend did not issue a verification token."))
    }

    private static func rowStatus(body: Data) -> String? {
        (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["status"] as? String
    }

    private static func detail(body: Data, fallback: String) -> String {
        if let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
           let detail = object["detail"] as? String, !detail.isEmpty {
            return detail
        }
        return fallback
    }
}

// MARK: - URLSession implementation (thin runner over the pure pieces)

/// Bearer-auth decisions over the app's existing channel. No new authority:
/// same token the API client uses, same routes, same reverify header.
public final class APIConfirmClient: ConfirmClient, Sendable {
    private let baseURL: URL
    private let token: String
    private let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    public func approve(ticketId: String, path: String, reason: String, reverify: String?) async throws -> ConfirmDecision {
        var request = URLRequest(url: ConfirmRequest.approveURL(base: baseURL, path: path, ticketId: ticketId))
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let reverify, !reverify.isEmpty {
            request.setValue(reverify, forHTTPHeaderField: "X-EV-Reverify")
        }
        request.httpBody = ConfirmRequest.decisionBody(reason: reason)
        request.timeoutInterval = 30
        return try await perform(ticketId: ticketId, request: request)
    }

    public func deny(ticketId: String, path: String, reason: String) async throws -> ConfirmDecision {
        var request = URLRequest(url: ConfirmRequest.denyURL(base: baseURL, path: path, ticketId: ticketId))
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = ConfirmRequest.decisionBody(reason: reason)
        request.timeoutInterval = 30
        return try await perform(ticketId: ticketId, request: request)
    }

    public func mintReverify() async throws -> String {
        var request = URLRequest(url: ConfirmRequest.reverifyURL(base: baseURL))
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = ConfirmRequest.reverifyBody()
        request.timeoutInterval = 30
        let (data, response) = try await send(request)
        guard let http = response as? HTTPURLResponse else {
            throw ConfirmError.requestFailed(message: "Non-HTTP response minting verification.")
        }
        guard (200...201).contains(http.statusCode) else {
            throw ConfirmError.requestFailed(message: "Verification mint failed (status \(http.statusCode)).")
        }
        switch ConfirmResponse.parseReverify(body: data) {
        case .success(let token): return token
        case .failure(let error): throw error
        }
    }

    private func perform(ticketId: String, request: URLRequest) async throws -> ConfirmDecision {
        let (data, response) = try await send(request)
        guard let http = response as? HTTPURLResponse else {
            throw ConfirmError.requestFailed(message: "Non-HTTP response deciding the ticket.")
        }
        switch ConfirmResponse.parseDecision(ticketId: ticketId, statusCode: http.statusCode, body: data) {
        case .success(let decision): return decision
        case .failure(let error): throw error
        }
    }

    private func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        do {
            return try await session.data(for: request)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw ConfirmError.requestFailed(message: "Network error: \(error.localizedDescription)")
        }
    }
}
