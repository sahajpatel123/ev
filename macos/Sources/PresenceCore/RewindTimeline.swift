import Combine
import Foundation

/// One rewind row: an approved action, a file-restore point, or (once CLI 1
/// ships `receipt_feed`) a turn receipt. A row offers Undo only when it
/// carries an `undoId` — everything else is read-only by construction.
public struct TimelineRow: Sendable, Equatable, Identifiable {
    public enum Kind: String, Sendable, Equatable {
        case action
        case restore
    }

    public var id: String
    public var at: Date?
    public var label: String
    public var target: String
    public var kind: Kind
    public var status: String
    public var verified: Bool?
    public var error: String?
    public var undoId: String?
    /// Per-row result line after a restore ("Restored …" / honest error).
    public var resultLine: String?
    public var resultOk: Bool?

    public init(
        id: String, at: Date? = nil, label: String = "", target: String = "",
        kind: Kind = .action, status: String = "", verified: Bool? = nil,
        error: String? = nil, undoId: String? = nil,
        resultLine: String? = nil, resultOk: Bool? = nil
    ) {
        self.id = id; self.at = at; self.label = label; self.target = target
        self.kind = kind; self.status = status; self.verified = verified
        self.error = error; self.undoId = undoId
        self.resultLine = resultLine; self.resultOk = resultOk
    }

    public static func from(action: TimelineAction) -> TimelineRow {
        let label = action.title.isEmpty ? action.actionType : action.title
        return TimelineRow(
            id: "action:\(action.id)", at: action.at, label: label,
            target: action.target.isEmpty ? action.actionType : action.target,
            kind: .action, status: action.status, verified: action.verified,
            error: action.error
        )
    }

    public static func from(entry: TimelineUndoEntry) -> TimelineRow {
        TimelineRow(
            id: "restore:\(entry.id)", at: entry.at,
            label: entry.rel.isEmpty ? "(restore point)" : entry.rel,
            target: entry.kind.isEmpty ? "file restore" : "file \(entry.kind)",
            kind: .restore, status: "restorable", undoId: entry.id
        )
    }
}

// MARK: - Pure filter (checked headless)

public enum TimelineScope: String, Sendable, Equatable {
    case today
    case all
}

public enum TimelineStatusFilter: String, Sendable, Equatable {
    case all
    case done
    case attention
    case restores
}

public enum TimelineFilter {
    /// Newest first. `now` and `calendar` inject wall time for tests.
    public static func apply(
        rows: [TimelineRow], search: String, scope: TimelineScope,
        status: TimelineStatusFilter, verifiedOnly: Bool,
        now: Date = Date(), calendar: Calendar = .current
    ) -> [TimelineRow] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return rows.filter { row in
            if scope == .today, let at = row.at,
               !calendar.isDate(at, inSameDayAs: now) {
                return false
            }
            switch status {
            case .all: break
            case .done:
                guard row.status == "executed" || row.status == "approved" else { return false }
            case .attention:
                guard row.status == "failed" || row.status == "denied" || row.error != nil else { return false }
            case .restores:
                guard row.kind == .restore else { return false }
            }
            if verifiedOnly, row.verified != true { return false }
            if !query.isEmpty {
                let hay = "\(row.label) \(row.target) \(row.status)".lowercased()
                if !hay.contains(query) { return false }
            }
            return true
        }
        .sorted { ($0.at ?? .distantPast) > ($1.at ?? .distantPast) }
    }
}

// MARK: - Model

@MainActor
public final class RewindTimelineModel: ObservableObject {
    @Published public private(set) var rows: [TimelineRow] = []
    @Published public private(set) var loading = false
    @Published public private(set) var errorLine: String?
    @Published public var search = ""
    @Published public var scope: TimelineScope = .today
    @Published public var statusFilter: TimelineStatusFilter = .all
    @Published public var verifiedOnly = false
    @Published public private(set) var restoringIds = Set<String>()

    private var client: TimelineClient
    private let now: () -> Date

    public init(client: TimelineClient, now: @escaping () -> Date = Date.init) {
        self.client = client
        self.now = now
    }

    public func setClient(_ client: TimelineClient) {
        self.client = client
    }

    public var visibleRows: [TimelineRow] {
        TimelineFilter.apply(
            rows: rows, search: search, scope: scope,
            status: statusFilter, verifiedOnly: verifiedOnly, now: now()
        )
    }

    public func refresh() async {
        loading = true
        errorLine = nil
        defer { loading = false }
        do {
            async let actions = client.fetchActions(limit: 100)
            async let entries = client.fetchUndoEntries()
            let (fetchedActions, fetchedEntries) = try await (actions, entries)
            // Preserve per-row result lines across refreshes.
            let prior = Dictionary(uniqueKeysWithValues: rows.map { ($0.id, $0) })
            rows = (fetchedActions.map(TimelineRow.from)
                + fetchedEntries.map(TimelineRow.from)).map { row in
                var next = row
                next.resultLine = prior[row.id]?.resultLine
                next.resultOk = prior[row.id]?.resultOk
                return next
            }
        } catch is CancellationError {
            // Silent: a superseded refresh is not news.
        } catch let error as TimelineError {
            errorLine = error.message
        } catch {
            errorLine = "Couldn't load the timeline: \(error.localizedDescription)"
        }
    }

    /// Restore one file row. The result lands on the row itself, honestly;
    /// the list refreshes after so a consumed restore point disappears.
    public func restore(_ row: TimelineRow) async {
        guard let undoId = row.undoId, !restoringIds.contains(row.id) else { return }
        restoringIds.insert(row.id)
        defer { restoringIds.remove(row.id) }
        do {
            _ = try await client.restoreUndo(id: undoId)
            setResult(rowId: row.id, ok: true, line: "Restored \(row.label).")
            await refresh()
        } catch is CancellationError {
            // Silent.
        } catch let error as TimelineError {
            setResult(rowId: row.id, ok: false, line: error.message)
        } catch {
            setResult(rowId: row.id, ok: false, line: "Restore failed: \(error.localizedDescription)")
        }
    }

    private func setResult(rowId: String, ok: Bool, line: String) {
        guard let index = rows.firstIndex(where: { $0.id == rowId }) else { return }
        rows[index].resultLine = line
        rows[index].resultOk = ok
    }
}
