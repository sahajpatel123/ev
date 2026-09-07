import EvieNativeBroker
import Foundation

@main
struct EvieBrokerCheck {
    static func main() {
        var failed = 0
        func check(_ name: String, _ ok: Bool) {
            if ok {
                print("PASS \(name)")
            } else {
                print("FAIL \(name)")
                failed += 1
            }
        }

        check("instagram-alias", AppLaunchRegistry.resolve("Insta")?.appID == "instagram")
        check("spotify", AppLaunchRegistry.resolve("Spotify")?.appID == "spotify")
        check("unknown-app", AppLaunchRegistry.resolve("BankOfNowhere") == nil)

        check("origin-localhost", TrustedOrigin.allows(URL(string: "http://127.0.0.1:8000/evie/")!))
        check("origin-tsnet", TrustedOrigin.allows(URL(string: "https://home.example.ts.net/evie/")!))
        check("origin-reject-external", !TrustedOrigin.allows(URL(string: "https://evil.example/evie/")!))

        check("reject-selector", NativeBridgeRequest.parse(["type": "invokeSelector"]) == nil)
        check("allow-haptic", NativeBridgeRequest.parse(["type": "haptic", "event": "selection"]) != nil)
        check("allow-pending-capture", NativeBridgeRequest.parse(["type": "pending_capture"]) != nil)
        check("allow-healthkit-snapshot", NativeBridgeRequest.parse(["type": "healthkit_snapshot"]) != nil)
        check("allow-calendar-snapshot", NativeBridgeRequest.parse(["type": "calendar_snapshot"]) != nil)
        check("allow-contacts-snapshot", NativeBridgeRequest.parse(["type": "contacts_snapshot"]) != nil)
        check("allow-notification-status", NativeBridgeRequest.parse(["type": "notification_status"]) != nil)
        check("allow-permission-status", NativeBridgeRequest.parse(["type": "permissionStatus"]) != nil)
        check("reject-eval", NativeBridgeRequest.parse(["type": "eval"]) == nil)

        let receipt = NativeReceipt(
            actionID: "ma_1",
            accepted: true,
            executed: false,
            verified: false,
            systemUIPresented: true,
            systemConfirmationRequired: true,
            result: "SYSTEM_UI_OPENED"
        )
        check("accepted-not-executed", receipt.accepted && !receipt.executed)
        check("executed-not-verified", !(receipt.executed && receipt.verified && receipt.result == "SYSTEM_UI_OPENED"))
        check("broker-version", BrokerVersion.version == "1.0.0")
        check("cycle48-orb-states", evieCycle48SelfTestCase())
        check("cycle78-planner", evieCycle78PlannerSelfTestCase())
        check("cycle79-outcome", evieCycle79OutcomeSelfTestCase())

        if failed > 0 {
            fputs("EvieBrokerCheck failed \(failed) assertion(s)\n", stderr)
            exit(1)
        }
        print("EvieBrokerCheck OK")
    }
}

// Cycle 48 — iPhone-only, backward compat: pure broker-check self-test case.
// Free function with no harness changes to existing checks; only adds one case above.
func evieCycle48SelfTestCase() -> Bool {
    let states = EvieOrbState.allCases.map(\.rawValue)
    assert(!states.isEmpty, "Cycle 48: orb states must not be empty")
    assert(Set(states).count == states.count, "Cycle 48: orb states must be unique")
    return !states.isEmpty && Set(states).count == states.count
}

// Cycle 78 — iPhone-only, backward compat: pure action-planner self-test.
func evieCycle78PlannerSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL cycle78: \(name)") }
    }
    let all = Set(["contacts", "health", "notifications", "calendar", "reminders"])
    expect("haptic-local", EvieActionPlanner.plan(for: "haptic", grantedPermissions: []).kind == .local)
    let msg = EvieActionPlanner.plan(for: "send_message", grantedPermissions: [])
    expect("message-systemui", msg.kind == .systemUI)
    expect("message-permission", msg.permissionRequired == "contacts")
    expect("message-reason", msg.reason == "PERMISSION_REQUIRED")
    let msgOk = EvieActionPlanner.plan(for: "send_message", grantedPermissions: all)
    expect("message-granted", msgOk.permissionRequired == nil && msgOk.reason == nil)
    expect("timer-server", EvieActionPlanner.plan(for: "start_timer", grantedPermissions: []).kind == .serverRouted)
    expect("reminder-permission", EvieActionPlanner.permission(for: "set_reminder") == "reminders")
    expect("unknown", EvieActionPlanner.plan(for: "bank_heist", grantedPermissions: all).kind == .unavailable)
    expect("empty", EvieActionPlanner.plan(for: "  ", grantedPermissions: all).reason == "EMPTY_CAPABILITY")
    expect("case-normalized", EvieActionPlanner.plan(for: "Haptic", grantedPermissions: []).kind == .local)
    expect("missing-multi", EvieActionPlanner.missingPermissions(for: "send_message", granted: []).count == 1)
    expect("missing-none", EvieActionPlanner.missingPermissions(for: "start_timer", granted: []).isEmpty)
    return ok
}

// Cycle 79 — iPhone-only, backward compat: outcome-contract self-test.
func evieCycle79OutcomeSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL cycle79: \(name)") }
    }
    expect("failed-vocab", EvieOutcomeState(accepted: false, executed: false, verified: false).status == .failed)
    expect("queued-vocab", EvieOutcomeState(accepted: true, executed: false, verified: false, queued: true).status == .queued)
    expect("accepted-vocab", EvieOutcomeState(accepted: true, executed: false, verified: false).status == .accepted)
    expect("completed-vocab", EvieOutcomeState(accepted: true, executed: true, verified: true).status == .completed)
    expect("completed-unverified", EvieOutcomeState(accepted: true, executed: true, verified: false).status == .completed)
    expect("executed-needs-accepted", !EvieOutcomeState(accepted: false, executed: true, verified: true).isHonest)
    expect("verified-needs-executed", !EvieOutcomeState(accepted: true, executed: false, verified: true).isHonest)
    expect("honest-accepted", EvieOutcomeState(accepted: true, executed: false, verified: false).isHonest)
    expect("honest-completed", EvieOutcomeState(accepted: true, executed: true, verified: true).isHonest)
    expect("ambiguous-copy", EvieOutcomeContract.describe(status: .failed, errorCode: "AMBIGUOUS").contains("which one"))
    expect("queued-copy", EvieOutcomeContract.describe(status: .queued, errorCode: nil).contains("Queued"))
    expect("all-cases", EvieOutcomeStatus.allCases.count == 4)
    return ok
}
