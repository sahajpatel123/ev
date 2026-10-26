import Foundation

/// Pure permission-check report (D7): prompt-free status in, human lines +
/// exit code out. The interactive tool gathers `CGPreflightListenEventAccess`
/// (Input Monitoring) and `AXIsProcessTrusted` (Accessibility) for the
/// CALLER and feeds them here; the report never claims to know another
/// process's grants (preflight only sees the caller).
public enum PresencePermissionReport {
    public struct Result: Equatable {
        /// 0 when Input Monitoring is granted (the gesture gate); 1 otherwise.
        public var exitCode: Int32
        public var lines: [String]
        public init(exitCode: Int32, lines: [String]) {
            self.exitCode = exitCode
            self.lines = lines
        }
    }

    public static func tool(inputMonitoring: Bool, accessibility: Bool) -> Result {
        var lines: [String] = []
        lines.append("presence-check permissions (this tool's grants; prompt-free)")
        lines.append("  Input Monitoring: \(inputMonitoring ? "GRANTED" : "MISSING")")
        lines.append("  Accessibility: \(accessibility ? "GRANTED" : "MISSING")")
        if !inputMonitoring {
            lines.append("")
            lines.append("To grant Input Monitoring to this tool:")
            lines.append("  1. Open System Settings > Privacy & Security > Input Monitoring.")
            lines.append("  2. Add your terminal (or re-run until macOS prompts), then re-run.")
            lines.append("If a stale entry blocks the prompt: tccutil reset ListenEvent")
        }
        if !accessibility {
            lines.append("")
            lines.append("Accessibility is used by the app hotkey fallback.")
            lines.append("  System Settings > Privacy & Security > Accessibility > Evie.")
        }
        lines.append("")
        lines.append("Note: these are THIS tool's grants. Evie.app holds its own set —")
        lines.append("open the app menu bar panel > Permissions > Grant permissions.")
        if inputMonitoring {
            lines.append("PASS: calibration may proceed.")
        } else {
            lines.append("FAIL: grant Input Monitoring, then re-run.")
        }
        return Result(exitCode: inputMonitoring ? 0 : 1, lines: lines)
    }
}
