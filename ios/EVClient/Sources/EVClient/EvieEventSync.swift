/// Event-store v2 sync: snapshot + delta pull via device-gateway with a
/// persisted `stream_seq` cursor and a durable file outbox.
///
/// Contract: `GET /v1/device-gateway/sync/bootstrap` (snapshot + `sync_cursor_str`)
/// then `GET /v1/device-gateway/sync/changes?cursor=` (bounded delta, `next_cursor`,
/// `has_more`, `events[]`). Cursor shape v2: `"v2|{epoch}|{stream_seq}"`.
/// Server reset signals (`CURSOR_INVALID`, `CURSOR_FORMAT_UPGRADE`,
/// `STATE_EPOCH_MISMATCH` + `reset_required`) mean: discard cursor, re-bootstrap.
///
/// Exactly-once replay: deltas are applied idempotently by event id — a write
/// landing mid-snapshot may appear in BOTH snapshot and delta. The pull loop
/// advances the persisted cursor only after the page is applied, and pages with
/// `has_more` until drained. `send` is GET-only retried inside EVAPIClient;
/// the outbox (pending local mutations) flushes separately with idempotency
/// keys so replays are server-deduplicated (201 applied / 409 duplicate).
///
/// Cursor persistence: Keychain (`evie-sync-cursor` account) first, UserDefaults
/// (`com.evie.syncCursor.v2`) as fallback mirror. Outbox: JSONL file surviving restart.

import Foundation

// MARK: - Models

public struct EvieSyncEvent: Codable, Sendable, Equatable {
    public let id: String
    public let eventType: String?
    public let source: String?
    public let streamSeq: Int?
    public let occurredAt: String?
    public let payload: [String: AnyCodable]?

    enum CodingKeys: String, CodingKey {
        case id
        case eventType = "event_type"
        case source
        case streamSeq = "stream_seq"
        case occurredAt = "occurred_at"
        case payload
    }
}

public struct EvieSyncPage: Sendable, Equatable {
    public let events: [EvieSyncEvent]
    public let nextCursor: String?
    public let hasMore: Bool
    public let resetRequired: Bool
    public let resetError: String?

    public init(events: [EvieSyncEvent], nextCursor: String?, hasMore: Bool, resetRequired: Bool = false, resetError: String? = nil) {
        self.events = events
        self.nextCursor = nextCursor
        self.hasMore = hasMore
        self.resetRequired = resetRequired
        self.resetError = resetError
    }
}

public struct EvieSyncSnapshot: Sendable {
    public let cursor: String?
    public let raw: [String: AnyCodable]

    public init(cursor: String?, raw: [String: AnyCodable]) {
        self.cursor = cursor
        self.raw = raw
    }
}

// MARK: - Cursor store (Keychain primary, UserDefaults mirror)

public struct EvieSyncCursorStore: Sendable {
    private static let keychainAccount = "evie-sync-cursor"
    private static let defaultsKey = "com.evie.syncCursor.v2"

    public let keychain: KeychainTokenStore
    public let defaults: UserDefaults

    public init(keychain: KeychainTokenStore = KeychainTokenStore(), defaults: UserDefaults = .standard) {
        self.keychain = keychain
        self.defaults = defaults
    }

    public func load() -> String? {
        if let token = try? keychain.load(account: Self.keychainAccount), !token.isEmpty {
            return token
        }
        let fallback = defaults.string(forKey: Self.defaultsKey)
        if let fallback, !fallback.isEmpty {
            try? keychain.save(token: fallback, account: Self.keychainAccount)
            return fallback
        }
        return nil
    }

    public func save(_ cursor: String?) {
        if let cursor, !cursor.isEmpty {
            try? keychain.save(token: cursor, account: Self.keychainAccount)
            defaults.set(cursor, forKey: Self.defaultsKey)
        } else {
            try? keychain.delete(account: Self.keychainAccount)
            defaults.removeObject(forKey: Self.defaultsKey)
        }
    }

    public func clear() { save(nil) }

    /// Client-side sanity check for the v2 shape (`v2|epoch|seq`).
    public static func isV2Cursor(_ raw: String?) -> Bool {
        guard let raw, !raw.isEmpty else { return false }
        let parts = raw.split(separator: "|", omittingEmptySubsequences: false)
        return parts.count == 3 && parts[0] == "v2" && Int(parts[2]) != nil
    }
}

// MARK: - Durable file outbox (survives restart)

/// Pending local mutations flushed with idempotency keys. JSONL file, atomic rewrites.
public struct EvieEventOutboxRecord: Codable, Sendable, Equatable {
    public let idempotencyKey: String
    public let queuedAt: String
    public let path: String
    public let method: String
    public let body: [String: AnyCodable]?

    public init(idempotencyKey: String = UUID().uuidString, queuedAt: String = Date().ISO8601Format(), path: String, method: String = "POST", body: [String: AnyCodable]? = nil) {
        self.idempotencyKey = idempotencyKey
        self.queuedAt = queuedAt
        self.path = path
        self.method = method
        self.body = body
    }
}

public final class EvieEventOutbox: @unchecked Sendable {
    private let fileURL: URL
    private let lock = NSLock()

    public init(directory: URL) {
        fileURL = directory.appendingPathComponent("event-outbox.jsonl")
    }

    public static func `default`() -> EvieEventOutbox {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return EvieEventOutbox(directory: base.appendingPathComponent("EVClient", isDirectory: true))
    }

    public func load() -> [EvieEventOutboxRecord] {
        lock.lock(); defer { lock.unlock() }
        return loadLocked()
    }

    public func enqueue(path: String, method: String = "POST", body: [String: AnyCodable]? = nil, idempotencyKey: String = UUID().uuidString) -> EvieEventOutboxRecord {
        lock.lock(); defer { lock.unlock() }
        var records = loadLocked()
        let record = EvieEventOutboxRecord(idempotencyKey: idempotencyKey, path: path, method: method, body: body)
        records.append(record)
        try? saveLocked(records)
        return record
    }

    /// Flush in FIFO order. 2xx/409 removes the record (409 = already applied);
    /// 4xx validation quarantine also removes it; transport errors stop and preserve the rest.
    @discardableResult
    public func flush(using client: EVAPIClient) async -> (flushed: Int, remaining: Int) {
        let records = load()
        guard !records.isEmpty else { return (0, 0) }
        var remaining = records
        var flushed = 0
        for record in records {
            do {
                let bodyData: Data? = try record.body.map { try JSONSerialization.data(withJSONObject: Dictionary(uniqueKeysWithValues: $0.map { ($0.key, $0.value.jsonObject()) })) }
                let (status, _) = try await client.send(
                    record.path,
                    method: record.method,
                    body: bodyData,
                    headers: ["Idempotency-Key": record.idempotencyKey],
                    allowedStatuses: [200, 201, 204, 400, 409, 422]
                )
                switch status {
                case 200, 201, 204, 409:
                    remaining.removeAll { $0.idempotencyKey == record.idempotencyKey }
                    flushed += 1
                case 400, 422:
                    remaining.removeAll { $0.idempotencyKey == record.idempotencyKey }
                default:
                    break
                }
            } catch {
                break // transport failure: preserve for next launch
            }
        }
        lock.lock(); defer { lock.unlock() }
        try? saveLocked(remaining)
        return (flushed, remaining.count)
    }

    // MARK: - Private (lock held)

    private func loadLocked() -> [EvieEventOutboxRecord] {
        guard FileManager.default.fileExists(atPath: fileURL.path),
              let data = try? Data(contentsOf: fileURL) else { return [] }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return data.split(separator: 0x0A).compactMap { line in
            try? decoder.decode(EvieEventOutboxRecord.self, from: Data(line))
        }
    }

    private func saveLocked(_ records: [EvieEventOutboxRecord]) throws {
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if records.isEmpty {
            try? FileManager.default.removeItem(at: fileURL)
            return
        }
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        var data = Data()
        for record in records {
            data.append(try encoder.encode(record))
            data.append(0x0A)
        }
        try data.write(to: fileURL, options: .atomic)
    }
}

// MARK: - Sync client (snapshot + delta pull)

public struct EvieEventSync: Sendable {
    public let client: EVAPIClient
    public let cursors: EvieSyncCursorStore

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    public init(client: EVAPIClient, cursors: EvieSyncCursorStore = EvieSyncCursorStore()) {
        self.client = client
        self.cursors = cursors
    }

    /// Snapshot pull. Persists `sync_cursor_str` (or legacy `cursor_str`) before returning.
    public func bootstrap() async throws -> EvieSyncSnapshot {
        let (_, data) = try await client.send("/v1/device-gateway/sync/bootstrap", allowedStatuses: [200])
        let decoded = try? JSONDecoder().decode([String: AnyCodable].self, from: data)
        let raw = decoded ?? [:]
        let cursor: String? = {
            if case .string(let s) = decoded?["sync_cursor_str"], !s.isEmpty { return s }
            if case .string(let s) = decoded?["cursor_str"], !s.isEmpty { return s }
            return nil
        }()
        if let cursor, !cursor.isEmpty {
            cursors.save(cursor)
        }
        return EvieSyncSnapshot(cursor: cursor, raw: raw)
    }

    /// One bounded delta page. On reset signals returns `resetRequired` without advancing the cursor.
    public func changes(cursor: String? = nil) async throws -> EvieSyncPage {
        let effective = cursor ?? cursors.load()
        var items: [URLQueryItem] = []
        if let effective, !effective.isEmpty {
            items.append(URLQueryItem(name: "cursor", value: effective))
        }
        let (_, data) = try await client.send("/v1/device-gateway/sync/changes", queryItems: items, allowedStatuses: [200])
        let object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
        if let ok = object["ok"] as? Bool, !ok {
            let error = object["error"] as? String ?? "CURSOR_INVALID"
            let reset = (object["reset_required"] as? Bool) ?? true
            return EvieSyncPage(events: [], nextCursor: effective, hasMore: false, resetRequired: reset, resetError: error)
        }
        let events: [EvieSyncEvent]
        if let array = object["events"] as? [[String: Any]],
           let arrayData = try? JSONSerialization.data(withJSONObject: array),
           let decoded = try? Self.decoder.decode([EvieSyncEvent].self, from: arrayData) {
            events = decoded
        } else {
            events = []
        }
        let next = object["next_cursor"] as? String ?? effective
        let hasMore = object["has_more"] as? Bool ?? false
        return EvieSyncPage(events: events, nextCursor: next, hasMore: hasMore)
    }

    /// Drain deltas to quiescence, advancing the persisted cursor only after `apply`
    /// succeeds for each page. `apply` MUST be idempotent by event id. On reset,
    /// re-bootstraps once and continues.
    public func pullToQuiescence(
        maxPages: Int = 20,
        apply: @Sendable ([EvieSyncEvent]) async throws -> Void
    ) async throws -> Int {
        var total = 0
        var pages = 0
        var didReset = false
        while pages < maxPages {
            pages += 1
            let page = try await changes()
            if page.resetRequired {
                guard !didReset else { return total }
                didReset = true
                cursors.clear()
                _ = try await bootstrap()
                continue
            }
            if !page.events.isEmpty {
                try await apply(page.events)
                total += page.events.count
            }
            if let next = page.nextCursor {
                cursors.save(next)
            }
            if !page.hasMore { break }
        }
        return total
    }
}
