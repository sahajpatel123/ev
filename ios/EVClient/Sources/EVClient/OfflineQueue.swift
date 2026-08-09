/// Offline-first capture queue: pending events persist locally with idempotency
/// keys and sync with the same contract as the CLI (`ev sync`) and web client:
/// 201 synced, 409 duplicate dropped, 422 quarantined, network failure preserved.

import Foundation

public struct PendingCapture: Codable, Sendable, Equatable {
    public let idempotencyKey: String
    public let queuedAt: String
    public let payload: CapturePayload

    public init(idempotencyKey: String, queuedAt: String, payload: CapturePayload) {
        self.idempotencyKey = idempotencyKey
        self.queuedAt = queuedAt
        self.payload = payload
    }
}

public protocol CaptureQueueStore: Sendable {
    func load() throws -> [PendingCapture]
    func save(_ records: [PendingCapture]) throws
    func append(_ record: PendingCapture) throws
    func quarantine(_ record: PendingCapture, reason: String) throws
}

public final class MemoryCaptureQueueStore: CaptureQueueStore, @unchecked Sendable {
    private let lock = NSLock()
    private var records: [PendingCapture] = []
    private var quarantined: [String] = []

    public init() {}

    public func load() throws -> [PendingCapture] {
        lock.lock()
        defer { lock.unlock() }
        return records
    }

    public func save(_ records: [PendingCapture]) throws {
        lock.lock()
        defer { lock.unlock() }
        self.records = records
    }

    public func append(_ record: PendingCapture) throws {
        lock.lock()
        defer { lock.unlock() }
        records.append(record)
    }

    public func quarantine(_ record: PendingCapture, reason: String) throws {
        lock.lock()
        defer { lock.unlock() }
        quarantined.append("\(record.idempotencyKey): \(reason)")
    }

    public func quarantinedCount() -> Int {
        lock.lock()
        defer { lock.unlock() }
        return quarantined.count
    }
}

public struct FileCaptureQueueStore: CaptureQueueStore {
    public let queueURL: URL
    public let quarantineURL: URL

    public init(directory: URL) {
        queueURL = directory.appendingPathComponent("captures.jsonl")
        quarantineURL = directory.appendingPathComponent("quarantine.jsonl")
    }

    public func load() throws -> [PendingCapture] {
        guard FileManager.default.fileExists(atPath: queueURL.path) else {
            return []
        }
        let data = try Data(contentsOf: queueURL)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try data.split(separator: 0x0A).map { line in
            try decoder.decode(PendingCapture.self, from: Data(line))
        }
    }

    public func save(_ records: [PendingCapture]) throws {
        try FileManager.default.createDirectory(
            at: queueURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if records.isEmpty {
            try? FileManager.default.removeItem(at: queueURL)
            return
        }
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let lines = try records.map { try encoder.encode($0) }
        var data = Data()
        for line in lines {
            data.append(line)
            data.append(0x0A)
        }
        try data.write(to: queueURL, options: .atomic)
    }

    public func append(_ record: PendingCapture) throws {
        let current = try load()
        try save(current + [record])
    }

    public func quarantine(_ record: PendingCapture, reason: String) throws {
        let line = "\(record.idempotencyKey) \(reason.replacingOccurrences(of: "\n", with: " "))\n"
        if let handle = FileHandle(forWritingAtPath: quarantineURL.path) {
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: Data(line.utf8))
        } else {
            try FileManager.default.createDirectory(
                at: quarantineURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try Data(line.utf8).write(to: quarantineURL)
        }
    }
}

public struct SyncSummary: Sendable, Equatable {
    public let synced: Int
    public let dropped: Int
    public let quarantined: Int
    public let remaining: Int
    public let errors: [String]

    public init(
        synced: Int,
        dropped: Int,
        quarantined: Int,
        remaining: Int,
        errors: [String] = []
    ) {
        self.synced = synced
        self.dropped = dropped
        self.quarantined = quarantined
        self.remaining = remaining
        self.errors = errors
    }
}

public struct OfflineCaptureQueue: Sendable {
    public let store: CaptureQueueStore

    public init(store: CaptureQueueStore) {
        self.store = store
    }

    public func pending() throws -> [PendingCapture] {
        try store.load()
    }

    public func enqueue(
        _ payload: CapturePayload,
        idempotencyKey: String = UUID().uuidString
    ) throws -> PendingCapture {
        let record = PendingCapture(
            idempotencyKey: idempotencyKey,
            queuedAt: Date().ISO8601Format(),
            payload: payload
        )
        try store.append(record)
        return record
    }

    public func sync(using client: EVAPIClient) async -> SyncSummary {
        let records: [PendingCapture]
        do {
            records = try store.load()
        } catch {
            return SyncSummary(
                synced: 0,
                dropped: 0,
                quarantined: 0,
                remaining: 0,
                errors: ["queue load failed: \(error)"]
            )
        }
        guard !records.isEmpty else {
            return SyncSummary(synced: 0, dropped: 0, quarantined: 0, remaining: 0)
        }

        var synced = 0
        var dropped = 0
        var quarantined = 0
        var remaining: [PendingCapture] = []
        var errors: [String] = []

        for record in records {
            do {
                let (status, data) = try await client.postEvent(
                    payload: record.payload,
                    idempotencyKey: record.idempotencyKey
                )
                switch status {
                case 201:
                    synced += 1
                case 409:
                    dropped += 1
                case 400, 422:
                    quarantined += 1
                    try? store.quarantine(
                        record,
                        reason: String(data: data, encoding: .utf8) ?? "validation failed"
                    )
                default:
                    remaining.append(record)
                    errors.append("\(record.idempotencyKey): HTTP \(status)")
                }
            } catch {
                remaining.append(record)
                errors.append("\(record.idempotencyKey): \(error)")
                break
            }
        }

        try? store.save(remaining)
        return SyncSummary(
            synced: synced,
            dropped: dropped,
            quarantined: quarantined,
            remaining: remaining.count,
            errors: errors
        )
    }
}
