import Foundation
import PresenceCore

/// Scripted-stream replay: every D6 story through the real reducer,
/// asserting the end state each story promises.
enum FixtureReplayChecks {
    static func drain(_ steps: [FixtureTransport.Step]) async throws -> PresenceSnapshot {
        var snapshot = PresenceSnapshot()
        for try await item in FixtureTransport(steps: steps).run() {
            snapshot.apply(item)
        }
        return snapshot
    }

    static func run(_ check: CheckFn) async {
        do {
            let six = try await drain(PresenceScripts.sixStepTask(paceMs: 0))
            if case .completed(let label) = six.pill {
                check(
                    "replay-sixstep",
                    label.contains("Triaged") && six.turn?.steps.count == 6
                        && six.turn?.captions.count == 2,
                    "\(six.pill)"
                )
            } else {
                check("replay-sixstep", false, "\(six.pill)")
            }
        } catch {
            check("replay-sixstep", false, "\(error)")
        }

        do {
            let confirm = try await drain(PresenceScripts.sendNeedingConfirm(paceMs: 0))
            if case .needsConfirm = confirm.pill,
               let ticket = confirm.turn?.confirm {
                check(
                    "replay-confirm",
                    ticket.isRich && ticket.ticketId.hasPrefix("3fa85f64")
                        && confirm.turn?.steps.count == 4,
                    "rich=\(ticket.isRich)"
                )
            } else {
                check("replay-confirm", false, "\(confirm.pill)")
            }
        } catch {
            check("replay-confirm", false, "\(error)")
        }

        do {
            // Mid-script state (failure) and end state (recovery) both hold.
            let full = PresenceScripts.failureWithRecovery(paceMs: 0)
            var mid = PresenceSnapshot()
            var events = 0
            for try await item in FixtureTransport(steps: full).run() {
                mid.apply(item)
                events += 1
                if events == 7 { // connection + hello + 5 frames of the failing turn
                    if case .failed(let reason, let next) = mid.pill {
                        check(
                            "replay-failure-mid",
                            reason.contains("contacts") && !next.isEmpty,
                            "\(mid.pill)"
                        )
                    } else {
                        check("replay-failure-mid", false, "\(mid.pill)")
                    }
                }
            }
            if case .completed = mid.pill, mid.turn?.id == "ft-retry" {
                check("replay-failure-recovered", true)
            } else {
                check("replay-failure-recovered", false, "\(mid.pill)")
            }
        } catch {
            check("replay-failure-mid", false, "\(error)")
            check("replay-failure-recovered", false, "\(error)")
        }

        do {
            let bg = try await drain(PresenceScripts.backgroundUpdate(paceMs: 0))
            check(
                "replay-background",
                bg.pill == .idle && bg.backgroundNotes.count == 2
                    && bg.turn == nil,
                "\(bg.pill) notes=\(bg.backgroundNotes.count)"
            )
        } catch {
            check("replay-background", false, "\(error)")
        }

        do {
            let panic = try await drain(PresenceScripts.panic(paceMs: 0))
            if case .failed(let reason, _) = panic.pill {
                check(
                    "replay-panic",
                    !panic.panic.panicked && reason.contains("panic"),
                    "\(panic.pill) panicked=\(panic.panic.panicked)"
                )
            } else {
                check("replay-panic", false, "\(panic.pill)")
            }
        } catch {
            check("replay-panic", false, "\(error)")
        }

        // Determinism: the same script twice yields equal snapshots.
        do {
            let first = try await drain(PresenceScripts.sixStepTask(paceMs: 0))
            let second = try await drain(PresenceScripts.sixStepTask(paceMs: 0))
            check("replay-deterministic", first == second)
        } catch {
            check("replay-deterministic", false, "\(error)")
        }

        // Store.attach wiring smoke: instant replay lands in the store.
        do {
            let store = await PresenceStore()
            await store.attach(FixtureTransport(steps: PresenceScripts.backgroundUpdate(paceMs: 0)))
            try? await Task.sleep(nanoseconds: 300_000_000)
            let notes = await store.snapshot.backgroundNotes.count
            await store.detach()
            check("replay-store-attach", notes == 2, "notes=\(notes)")
        }
    }
}
