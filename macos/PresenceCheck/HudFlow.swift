import AppKit
import Foundation
import PresenceCore

/// HUD visibility check (owner-run, interactive): cycles the REAL pill
/// through a live paced script, asserts the panel contract
/// programmatically, then asks the owner to confirm with their eyes.
enum HudFlow {
    static func run() -> Int32 {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        return MainActor.assumeIsolated {
            let store = PresenceStore()
            let pill = HUDPillController()
            pill.attach(store)
            print("=== HUD visibility check ===")
            print("Cycling the real pill through a 6-step task (~7 s)…")
            store.attach(FixtureTransport(steps: PresenceScripts.sixStepTask(paceMs: 350)))
            RunLoop.main.run(until: Date().addingTimeInterval(8))
            var failures: [String] = []
            if let facts = pill.panelFacts() {
                print("  panel visible: \(facts.visible)")
                print("  floating level: \(facts.levelRaw == Int(NSWindow.Level.floating.rawValue))")
                print("  all Spaces: \(facts.canJoinAllSpaces)")
                print("  nonactivating: \(facts.isNonactivating)")
                if !facts.visible { failures.append("panel not visible") }
                if facts.levelRaw != Int(NSWindow.Level.floating.rawValue) {
                    failures.append("panel is not floating")
                }
                if !facts.canJoinAllSpaces { failures.append("panel is not on all Spaces") }
                if !facts.isNonactivating { failures.append("panel is not nonactivating") }
            } else {
                failures.append("no panel was created")
            }
            let pillState = store.snapshot.pill
            print("  end state: \(pillState)")
            if pillState == .idle {
                failures.append("pill is idle after the script (expected the completed outcome)")
            }
            store.detach()
            print("")
            print("Look at the top of the screen now: the pill should be gone (detached).")
            print("Was the pill visible during the task, with plan steps when expanded? [y/N]",
                  terminator: " ")
            let answer = readLine()?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if answer != "y" && answer != "yes" {
                failures.append("owner did not confirm visibility")
            }
            if failures.isEmpty {
                print("PASS: HUD is visible and well-behaved.")
                return Int32(0)
            }
            print("FAIL:")
            for failure in failures {
                print("  - \(failure)")
            }
            return Int32(1)
        }
    }
}
