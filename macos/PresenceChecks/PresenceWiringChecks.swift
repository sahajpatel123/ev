import Foundation

/// Source-level wiring checks for presence seams that cannot run headless
/// (the EV target is not importable from checks). Follows the EVMicTalkTests
/// `wired-*` pattern: read the shipped source and assert the markers exist.
enum PresenceWiringChecks {
    static func macosRoot() -> URL {
        let here = URL(fileURLWithPath: #filePath)
        return here
            .deletingLastPathComponent() // file
            .deletingLastPathComponent() // PresenceChecks
    }

    static func run(_ check: CheckFn) {
        let root = macosRoot()
        func read(_ rel: String) -> String? {
            try? String(contentsOf: root.appendingPathComponent(rel), encoding: .utf8)
        }
        guard let appModel = read("Sources/EV/AppModel.swift") else {
            check("wired-appmodel-readable", false, root.path)
            return
        }
        check("wired-appmodel-readable", true)
        check("wired-gestureTap-property", appModel.contains("let gestureTap = OptionGestureTap()"))
        check("wired-gestureTap-start", appModel.contains("gestureTap.start()"))
        check("wired-presenceStore-property", appModel.contains("let presenceStore = PresenceStore()"))
        check("wired-hudPill-attach", appModel.contains("hudPill.attach(presenceStore)"))
        check("wired-looking-monitor-start", appModel.contains("lookingAtMonitor.start()"))
        check("wired-levels-sink", appModel.contains("presenceLevels.updateInput"))
        check(
            "wired-gesture-default-off",
            (read("Sources/EV/OptionGestureTap.swift") ?? "").contains("guard Self.settingEnabled else")
        )
        check(
            "wired-tap-listen-only",
            (read("Sources/EV/OptionGestureTap.swift") ?? "").contains("options: .listenOnly")
        )
        let package = read("Package.swift") ?? ""
        check("wired-package-core", package.contains("\"PresenceCore\""))
        check("wired-package-gesture", package.contains("\"PresenceGesture\""))
    }
}
