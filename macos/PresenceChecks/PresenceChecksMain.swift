import Foundation
import PresenceGesture

/// Presence checks: gesture engine + typing replay + (later) presence suites.
///
/// Command Line Tools here have no XCTest, so this executable is the in-repo
/// test, following the EVMicTalkTests pattern:
/// `swift run --package-path macos PresenceChecks`. Nonzero exit on failure.
@main
enum PresenceChecks {
    static func main() async {
        var passed = 0
        var failed = 0
        func check(_ name: String, _ condition: @autoclosure () -> Bool, _ detail: String = "") {
            if condition() {
                passed += 1
                print("PASS \(name)")
            } else {
                failed += 1
                print("FAIL \(name)\(detail.isEmpty ? "" : " — \(detail)")")
            }
        }

        GestureEngineChecks.run { name, ok, detail in
            check(name, ok, detail)
        }
        let checkFn = CheckFn { name, ok, detail in
            check(name, ok, detail)
        }
        TypingReplay.run(checkFn)
        PresenceEventChecks.run(checkFn)
        PresenceStoreChecks.run(checkFn)
        PresenceWiringChecks.run(checkFn)
        ConfirmPolicyChecks.run(checkFn)
        ConfirmClientChecks.run(checkFn)
        await ConfirmCardModelChecks.run(checkFn)
        TimelineParserChecks.run(checkFn)
        TimelineFilterChecks.run(checkFn)
        await RewindModelChecks.run(checkFn)
        SSEParserChecks.run(checkFn)
        await FixtureReplayChecks.run(checkFn)
        CalibrationChecks.run(checkFn)
        PermissionReportChecks.run(checkFn)
        // Snapshot suites render AppKit/SwiftUI offscreen on the main actor;
        // no windows are ever ordered on screen.
        await HUDPillSnapshotChecks.run(checkFn)
        await ConfirmCardSnapshotChecks.run(checkFn)
        await RewindSnapshotChecks.run(checkFn)
        await ReplaySnapshotChecks.run(checkFn)

        print("presence-checks passed=\(passed) failed=\(failed)")
        if failed > 0 {
            exit(1)
        }
    }
}
