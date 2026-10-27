import ApplicationServices
import Foundation
import PresenceCore
import PresenceGesture

/// Prompt-free permission gate (owner-run). Reports THIS tool's grants and
/// exits 0 only when Input Monitoring is granted. Never prompts: prompting
/// mid-check would deadlock the guidance it is trying to give.
enum PermissionsFlow {
    static func run() -> Int32 {
        let report = PresencePermissionReport.tool(
            inputMonitoring: GestureTapSource.inputMonitoringGranted(),
            accessibility: AXIsProcessTrusted()
        )
        for line in report.lines {
            print(line)
        }
        return report.exitCode
    }
}
