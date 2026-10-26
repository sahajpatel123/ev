import Foundation
import PresenceCore

/// Scriptable fake timeline transport.
actor FakeTimelineClient: TimelineClient {
    var actions: [TimelineAction]
    var entries: [TimelineUndoEntry]
    var fetchError: TimelineError?
    var restoreHandler: [String: Result<String, TimelineError>]

    private(set) var fetchCount = 0
    private(set) var restoreCalls: [String] = []

    init(
        actions: [TimelineAction] = [],
        entries: [TimelineUndoEntry] = [],
        fetchError: TimelineError? = nil,
        restoreHandler: [String: Result<String, TimelineError>] = [:]
    ) {
        self.actions = actions
        self.entries = entries
        self.fetchError = fetchError
        self.restoreHandler = restoreHandler
    }

    func fetchActions(limit: Int) async throws -> [TimelineAction] {
        fetchCount += 1
        if let error = fetchError { throw error }
        return actions
    }

    func fetchUndoEntries() async throws -> [TimelineUndoEntry] {
        if let error = fetchError { throw error }
        return entries
    }

    func restoreUndo(id: String) async throws -> String {
        restoreCalls.append(id)
        switch restoreHandler[id] {
        case .success(let line): return line
        case .failure(let error): throw error
        case nil: return "Restored."
        }
    }
}

enum RewindModelChecks {
    static let now = Date(timeIntervalSince1970: 1_792_000_000)

    static func sample() -> (actions: [TimelineAction], entries: [TimelineUndoEntry]) {
        (
            actions: [
                TimelineAction(
                    id: "a1", at: now.addingTimeInterval(-300), title: "Text Sam",
                    actionType: "comms_act", status: "executed",
                    target: "Sam (Messages)", verified: true, ok: true
                ),
                TimelineAction(
                    id: "a2", at: now.addingTimeInterval(-90000), title: "Old report",
                    actionType: "files_op", status: "failed",
                    target: "report.md", verified: false, ok: false, error: "disk full"
                ),
            ],
            entries: [
                TimelineUndoEntry(
                    id: "root::notes.info.json", rel: "notes.md",
                    kind: "snapshot", at: now.addingTimeInterval(-600)
                ),
            ]
        )
    }

    static func run(_ check: CheckFn) async {
        // Refresh merges actions + restores; today scope is the default.
        do {
            let sample = sample()
            let fake = FakeTimelineClient(actions: sample.actions, entries: sample.entries)
            let model = await RewindTimelineModel(client: fake, now: { now })
            await model.refresh()
            let rows = await model.rows
            let visible = await model.visibleRows
            let scope = await model.scope
            check(
                "rewind-refresh",
                rows.count == 3 && scope == .today
                    && visible.map(\.id) == ["action:a1", "restore:root::notes.info.json"],
                "rows=\(rows.count) visible=\(visible.map(\.id))"
            )
            await model.setScopeAllForTests()
            let all = await model.visibleRows
            check("rewind-scope-all", all.count == 3, "\(all.count)")
        }
        // Fetch failure surfaces honestly; rows stay empty.
        do {
            let fake = FakeTimelineClient(fetchError: .requestFailed(message: "down"))
            let model = await RewindTimelineModel(client: fake, now: { now })
            await model.refresh()
            let error = await model.errorLine
            let rows = await model.rows
            check("rewind-fetch-error", error == "down" && rows.isEmpty, "\(String(describing: error))")
        }
        // Restore success: result line on the row + list refresh.
        do {
            let sample = sample()
            let fake = FakeTimelineClient(
                actions: sample.actions, entries: sample.entries,
                restoreHandler: ["root::notes.info.json": .success("Restored.")]
            )
            let model = await RewindTimelineModel(client: fake, now: { now })
            await model.refresh()
            let rows = await model.rows
            guard let target = rows.first(where: { $0.undoId != nil }) else {
                check("rewind-restore-success", false, "no restorable row")
                return
            }
            await model.restore(target)
            let after = await model.rows
            let line = after.first { $0.id == target.id }?.resultLine
            let fetches = await fake.fetchCount
            let calls = await fake.restoreCalls
            check(
                "rewind-restore-success",
                line == "Restored notes.md." && calls == ["root::notes.info.json"] && fetches == 2,
                "\(String(describing: line)) calls=\(calls) fetches=\(fetches)"
            )
        }
        // Restore failure: honest line, no refresh storm, row kept.
        do {
            let sample = sample()
            let fake = FakeTimelineClient(
                actions: sample.actions, entries: sample.entries,
                restoreHandler: ["root::notes.info.json": .failure(
                    .requestFailed(message: "That restore point is gone.")
                )]
            )
            let model = await RewindTimelineModel(client: fake, now: { now })
            await model.refresh()
            let rows = await model.rows
            guard let target = rows.first(where: { $0.undoId != nil }) else {
                check("rewind-restore-failure", false, "no restorable row")
                return
            }
            await model.restore(target)
            let after = await model.rows
            let row = after.first { $0.id == target.id }
            let fetches = await fake.fetchCount
            check(
                "rewind-restore-failure",
                row?.resultLine == "That restore point is gone."
                    && row?.resultOk == false && fetches == 1,
                "\(String(describing: row?.resultLine)) fetches=\(fetches)"
            )
        }
        // Action rows carry no Undo: restore() is a no-op without network.
        do {
            let sample = sample()
            let fake = FakeTimelineClient(actions: sample.actions, entries: sample.entries)
            let model = await RewindTimelineModel(client: fake, now: { now })
            await model.refresh()
            let rows = await model.rows
            guard let action = rows.first(where: { $0.kind == .action }) else {
                check("rewind-action-readonly", false, "no action row")
                return
            }
            await model.restore(action)
            let calls = await fake.restoreCalls
            check("rewind-action-readonly", calls.isEmpty)
        }
    }
}

@MainActor
extension RewindTimelineModel {
    /// Test seam: the view drives this through the scope picker.
    func setScopeAllForTests() {
        scope = .all
    }
}
