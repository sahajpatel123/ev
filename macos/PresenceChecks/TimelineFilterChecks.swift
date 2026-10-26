import Foundation
import PresenceCore

/// Timeline filter: scope, search, status, verified-only, newest-first.
/// Fixed clock + UTC calendar so day boundaries are deterministic.
enum TimelineFilterChecks {
    static func run(_ check: CheckFn) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: 1_792_000_000) // a Sunday noon UTC
        let yesterday = now.addingTimeInterval(-90000)
        let rows = [
            TimelineRow(id: "1", at: now.addingTimeInterval(-3600), label: "Text Sam",
                        target: "Sam (Messages)", kind: .action, status: "executed", verified: true),
            TimelineRow(id: "2", at: yesterday, label: "Text Sam", target: "Sam (Messages)",
                        kind: .action, status: "executed", verified: true),
            TimelineRow(id: "3", at: now.addingTimeInterval(-1800), label: "Run report query",
                        target: "shop.db", kind: .action, status: "failed",
                        verified: false, error: "no such table"),
            TimelineRow(id: "4", at: now.addingTimeInterval(-600), label: "notes.md",
                        target: "file snapshot", kind: .restore, status: "restorable", undoId: "u1"),
            TimelineRow(id: "5", at: nil, label: "Mystery row", target: "",
                        kind: .action, status: "approved"),
        ]
        func apply(_ rows: [TimelineRow], search: String = "", scope: TimelineScope = .all,
                   status: TimelineStatusFilter = .all, verifiedOnly: Bool = false) -> [TimelineRow] {
            TimelineFilter.apply(
                rows: rows, search: search, scope: scope, status: status,
                verifiedOnly: verifiedOnly, now: now, calendar: calendar
            )
        }
        let todayRows = apply(rows, scope: .today)
        check("filter-today", todayRows.map(\.id) == ["4", "3", "1", "5"], "\(todayRows.map(\.id))")
        // Dateless rows survive the today scope (no date to exclude by);
        // dated rows outside today do not.
        check("filter-today-keeps-dateless", todayRows.contains { $0.id == "5" })
        check("filter-today-drops-yesterday", !todayRows.contains { $0.id == "2" })
        check("filter-newest-first", apply(rows).map(\.id) == ["4", "3", "1", "2", "5"],
              "\(apply(rows).map(\.id))")
        check(
            "filter-search",
            apply(rows, search: "sam").map(\.id).sorted() == ["1", "2"]
                && apply(rows, search: "SHOP").map(\.id) == ["3"]
                && apply(rows, search: "snapshot").map(\.id) == ["4"]
        )
        check(
            "filter-status",
            apply(rows, status: .done).map(\.id).sorted() == ["1", "2", "5"]
                && apply(rows, status: .attention).map(\.id) == ["3"]
                && apply(rows, status: .restores).map(\.id) == ["4"]
        )
        check(
            "filter-verified-only",
            apply(rows, verifiedOnly: true).map(\.id).sorted() == ["1", "2"]
        )
        check(
            "filter-combined",
            apply(rows, search: "sam", scope: .today, verifiedOnly: true).map(\.id) == ["1"]
        )
    }
}
