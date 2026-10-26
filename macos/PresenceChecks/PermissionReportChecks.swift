import Foundation
import PresenceCore

/// Permission report matrix: every grant combination → exit code + lines.
/// The preflight calls themselves are owner-run only (they report the
/// caller's grants, meaningless from this sandbox).
enum PermissionReportChecks {
    static func run(_ check: CheckFn) {
        let full = PresencePermissionReport.tool(inputMonitoring: true, accessibility: true)
        check(
            "permrep-full",
            full.exitCode == 0 && full.lines.contains("PASS: calibration may proceed.")
                && !full.lines.joined(separator: "\n").contains("tccutil"),
            "exit=\(full.exitCode)"
        )
        let missingIM = PresencePermissionReport.tool(inputMonitoring: false, accessibility: true)
        check(
            "permrep-missing-im",
            missingIM.exitCode == 1
                && missingIM.lines.contains("FAIL: grant Input Monitoring, then re-run.")
                && missingIM.lines.joined(separator: "\n").contains("tccutil reset ListenEvent"),
            "exit=\(missingIM.exitCode)"
        )
        let missingAX = PresencePermissionReport.tool(inputMonitoring: true, accessibility: false)
        check(
            "permrep-missing-ax",
            missingAX.exitCode == 0
                && missingAX.lines.joined(separator: "\n").contains("Accessibility is used"),
            "exit=\(missingAX.exitCode)"
        )
        let none = PresencePermissionReport.tool(inputMonitoring: false, accessibility: false)
        check("permrep-none", none.exitCode == 1, "exit=\(none.exitCode)")
        // The report always scopes itself to the caller's grants.
        check(
            "permrep-honest-scope",
            none.lines.joined(separator: "\n").contains("THIS tool's grants"),
            none.lines.joined(separator: " / ")
        )
    }
}
