import Foundation
import PresenceGesture

/// Presence checks: gesture engine + typing replay + (later) presence suites.
///
/// Command Line Tools here have no XCTest, so this executable is the in-repo
/// test, following the EVMicTalkTests pattern:
/// `swift run --package-path macos PresenceChecks`. Nonzero exit on failure.
@main
enum PresenceChecks {
    static func main() {
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
        // Snapshot suites render AppKit/SwiftUI offscreen: main thread only.
        // This runner is single-threaded from process entry, so the main
        // thread IS the main actor here; no windows are ever ordered.
        MainActor.assumeIsolated {
            HUDPillSnapshotChecks.run(checkFn)
        }

        print("presence-checks passed=\(passed) failed=\(failed)")
        if failed > 0 {
            exit(1)
        }
    }
}
