import Foundation

/// Incremental SSE frame parser for the presence stream. Pure: lines in,
/// frames out. Comments (`:heartbeat`) and blank-line discipline follow the
/// SSE standard; malformed frames are reported as skips, never thrown, so
/// one bad frame cannot kill the stream.
public struct SSEFrameParser: Sendable {
    public struct Frame: Sendable, Equatable {
        public var id: String?
        public var event: String?
        public var data: String
        public init(id: String? = nil, event: String? = nil, data: String = "") {
            self.id = id; self.event = event; self.data = data
        }
    }

    public enum Output: Sendable, Equatable {
        case frame(Frame)
        case heartbeat
    }

    private var eventName = ""
    private var eventId: String?
    private var dataLines: [String] = []
    public private(set) var malformedCount = 0

    public init() {}

    /// Feed one stream line. Returns output only on frame completion.
    public mutating func feed(line: String) -> Output? {
        if line.hasPrefix(":") { return .heartbeat }
        if line.hasPrefix("event:") {
            flushPending()
            eventName = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
            return nil
        }
        if line.hasPrefix("data:") {
            dataLines.append(line.dropFirst(5).trimmingCharacters(in: .whitespaces))
            return nil
        }
        if line.hasPrefix("id:") {
            eventId = line.dropFirst(3).trimmingCharacters(in: .whitespaces)
            return nil
        }
        if line.isEmpty {
            return flush()
        }
        // Unknown field (e.g. retry:): ignored per SSE, frame continues.
        return nil
    }

    /// End of stream: flush a final unterminated frame, if any.
    public mutating func finish() -> Output? {
        flush()
    }

    private mutating func flushPending() {
        // A new `event:` line implicitly completes the previous frame only
        // when that frame had no blank terminator AND carried data. Without
        // data there is nothing to emit (matches Streaming.swift behavior).
        if !eventName.isEmpty || !dataLines.isEmpty {
            _ = flush()
        }
    }

    private mutating func flush() -> Output? {
        defer {
            eventName = ""
            eventId = nil
            dataLines = []
        }
        if eventName.isEmpty && dataLines.isEmpty { return nil }
        if dataLines.isEmpty {
            malformedCount += 1
            return nil
        }
        return .frame(Frame(
            id: eventId, event: eventName.isEmpty ? nil : eventName,
            data: dataLines.joined(separator: "\n")
        ))
    }
}

/// Reconnect backoff: 1s doubling to a 30s ceiling. Pure and exact; the
/// transport adds ±20% jitter on top (untimed, untested randomness).
public enum PresenceBackoff {
    public static func delay(attempt: Int) -> Double {
        min(1.0 * pow(2.0, Double(max(0, attempt))), 30.0)
    }
}

public enum PresenceStreamError: Error, Sendable {
    case authFailed(message: String)
}

/// The real presence transport: SSE `GET /v1/fabric/presence/stream` with
/// Bearer auth, `Last-Event-ID` resume, persisted cursor, heartbeat
/// watchdog, and reconnect backoff. Yields `.connection` transitions plus
/// contract events; malformed frames are skipped and counted (the count is
/// logged, never rendered as data).
public final class SSEPresenceTransport: PresenceTransport, @unchecked Sendable {
    private let baseURL: URL
    private let token: String
    private let deviceId: String
    private let session: URLSession
    private let cursorKey: String
    private let lock = NSLock()
    private var stopped = false

    public init(
        baseURL: URL, token: String, deviceId: String,
        session: URLSession? = nil, cursorKey: String = "EV_PRESENCE_CURSOR"
    ) {
        self.baseURL = baseURL
        self.token = token
        self.deviceId = deviceId
        self.session = session ?? Self.makeSession()
        self.cursorKey = cursorKey
    }

    /// Long-lived read session: waits for connectivity (Tailnet roams
    /// pause instead of failing) and never times out an idle stream.
    public static func makeSession() -> URLSession {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 90
        config.timeoutIntervalForResource = 7 * 24 * 3600
        config.waitsForConnectivity = true
        config.httpMaximumConnectionsPerHost = 2
        return URLSession(configuration: config)
    }

    public func stop() {
        lock.lock()
        stopped = true
        lock.unlock()
    }

    private var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }

    public var cursor: String? {
        get { UserDefaults.standard.string(forKey: cursorKey) }
        set {
            if let value = newValue {
                UserDefaults.standard.set(value, forKey: cursorKey)
            } else {
                UserDefaults.standard.removeObject(forKey: cursorKey)
            }
        }
    }

    public func run() -> AsyncThrowingStream<PresenceTransportEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                await self.pump(continuation)
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func pump(
        _ continuation: AsyncThrowingStream<PresenceTransportEvent, Error>.Continuation
    ) async {
        var attempt = 0
        continuation.yield(.connection(.connecting))
        while !isStopped, !Task.isCancelled {
            do {
                try await self.readOnce(continuation)
                attempt = 0 // a clean read resets the backoff
                if !self.isStopped {
                    // Server closed the stream: reconnect, do not spin.
                    continuation.yield(.connection(.reconnecting(note: "Stream closed — reconnecting…")))
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                }
            } catch is CancellationError {
                break
            } catch let error as PresenceStreamError {
                switch error {
                case .authFailed(let message):
                    continuation.yield(.connection(.offline(note: message)))
                    continuation.finish()
                    return
                }
            } catch {
                if self.isStopped || Task.isCancelled { break }
                let delay = PresenceBackoff.delay(attempt: attempt)
                attempt += 1
                continuation.yield(.connection(.reconnecting(
                    note: "Connection lost — retrying…"
                )))
                let jitter = delay * Double.random(in: 0.8...1.2)
                try? await Task.sleep(nanoseconds: UInt64(jitter * 1_000_000_000))
            }
        }
        continuation.finish()
    }

    private func readOnce(
        _ continuation: AsyncThrowingStream<PresenceTransportEvent, Error>.Continuation
    ) async throws {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("v1/fabric/presence/stream"),
            resolvingAgainstBaseURL: false
        )
        var items = [URLQueryItem(name: "device_id", value: deviceId)]
        if let cursor {
            items.append(URLQueryItem(name: "cursor", value: cursor))
        }
        components?.queryItems = items
        var request = URLRequest(url: components?.url ?? baseURL)
        request.httpMethod = "GET"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let cursor {
            request.setValue(cursor, forHTTPHeaderField: "Last-Event-ID")
        }
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw PresenceStreamError.authFailed(message: "Non-HTTP response from the backend.")
        }
        guard http.statusCode == 200 else {
            if http.statusCode == 401 {
                throw PresenceStreamError.authFailed(
                    message: "Backend auth failed — the app's credential is wrong."
                )
            }
            throw URLError(.badServerResponse)
        }
        continuation.yield(.connection(.live(note: nil)))
        var parser = SSEFrameParser()
        var sawFrame = false
        for try await line in bytes.lines {
            if isStopped || Task.isCancelled { break }
            guard let output = parser.feed(line: line) else { continue }
            switch output {
            case .heartbeat:
                continue
            case .frame(let frame):
                sawFrame = true
                if let id = frame.id {
                    cursor = id
                }
                if let event = Self.decode(frame: frame) {
                    continuation.yield(.event(event))
                }
            }
        }
        if let output = parser.finish(), case .frame(let frame) = output {
            sawFrame = true
            if let id = frame.id {
                cursor = id
            }
            if let event = Self.decode(frame: frame) {
                continuation.yield(.event(event))
            }
        }
        if parser.malformedCount > 0 {
            NSLog("[PRESENCE] skipped %d malformed stream frames", parser.malformedCount)
        }
        _ = sawFrame
    }

    /// Frame → contract event. Pure apart from the log: malformed JSON is
    /// skipped (nil), unknown types decode to `.unknown` via PresenceEvent.
    public static func decode(frame: SSEFrameParser.Frame) -> PresenceEvent? {
        guard let data = frame.data.data(using: .utf8) else { return nil }
        do {
            return try JSONDecoder().decode(PresenceEvent.self, from: data)
        } catch {
            NSLog("[PRESENCE] skipping undecodable frame id=%@", frame.id ?? "?")
            return nil
        }
    }
}
