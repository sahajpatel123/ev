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
        TypingReplay.run { name, ok, detail in
            check(name, ok, detail)
        }

        print("presence-checks passed=\(passed) failed=\(failed)")
        if failed > 0 {
            exit(1)
        }
    }
}
