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
        check("origin-tailscale-ip-diagnostics", TrustedOrigin.allows(URL(string: "http://100.90.1.2:8000/evie/")!))
        check("origin-lan-diagnostics", TrustedOrigin.allows(URL(string: "http://192.168.1.5:8000/evie/")!))
        check("origin-reject-public-ip", !TrustedOrigin.allows(URL(string: "http://8.8.8.8:8000/evie/")!))
        check("voice-https-tsnet", TrustedOrigin.isVoiceCapable(URL(string: "https://home.example.ts.net/evie/")!))
        check("voice-http-tailscale-ip-blocked", !TrustedOrigin.isVoiceCapable(URL(string: "http://100.90.1.2:8000/evie/")!))
        check("voice-http-loopback-ok", TrustedOrigin.isVoiceCapable(URL(string: "http://127.0.0.1:8000/evie/")!))

        check("reject-selector", NativeBridgeRequest.parse(["type": "invokeSelector"]) == nil)
        check("allow-haptic", NativeBridgeRequest.parse(["type": "haptic", "event": "selection"]) != nil)
        check("allow-pending-capture", NativeBridgeRequest.parse(["type": "pending_capture"]) != nil)
        check("allow-healthkit-snapshot", NativeBridgeRequest.parse(["type": "healthkit_snapshot"]) != nil)
        check("allow-calendar-snapshot", NativeBridgeRequest.parse(["type": "calendar_snapshot"]) != nil)
        check("allow-contacts-snapshot", NativeBridgeRequest.parse(["type": "contacts_snapshot"]) != nil)
        check("allow-notification-status", NativeBridgeRequest.parse(["type": "notification_status"]) != nil)
        check("allow-interpret-capture", NativeBridgeRequest.parse(["type": "interpret_capture"]) != nil)
        check("allow-mic-start", NativeBridgeRequest.parse(["type": "mic_start"]) != nil)
        check("allow-mic-read", NativeBridgeRequest.parse(["type": "mic_read", "timeout_ms": 400]) != nil)
        check("allow-mic-stop", NativeBridgeRequest.parse(["type": "mic_stop"]) != nil)
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
        check("mesh-routing", evieMeshSelfTestCase())
        check("sync-scheduler", evieSyncSchedulerSelfTestCase())
        check("capture-interpreter", evieCaptureInterpreterSelfTestCase())
        check("vision-policy", evieVisionPolicySelfTestCase())
        check("health-snapshot", evieHealthSnapshotSelfTestCase())
        check("call-planner", evieCallPlannerSelfTestCase())
        check("alarm-planner", evieAlarmPlannerSelfTestCase())
        check("poll-planner", eviePollPlannerSelfTestCase())
        check("message-channel", evieMessageChannelSelfTestCase())
        check("siri-phrases", evieSiriPhrasesSelfTestCase())

        check("bind-verify-path", BindSessionVerifier.verifyPath == "/v1/voice/hands-free/status")
        check("bind-200-stores", BindSessionVerifier.decide(statusCode: 200) == .verifiedStore)
        check("bind-401-deletes", BindSessionVerifier.decide(statusCode: 401) == .rejectedDelete)
        check("bind-403-deletes", BindSessionVerifier.decide(statusCode: 403) == .rejectedDelete)
        check("bind-offline-keeps", BindSessionVerifier.decide(statusCode: nil) == .unverifiedKeep)
        check("bind-500-keeps", BindSessionVerifier.decide(statusCode: 500) == .unverifiedKeep)
        check("bind-404-keeps", BindSessionVerifier.decide(statusCode: 404) == .unverifiedKeep)

        check("token-convention-service", SharedTokenConvention.service == "com.ev.client.tokens")
        check("token-convention-account", SharedTokenConvention.account == "api")
        check("token-convention-group", SharedTokenConvention.groupSuffix == "com.ev.ios")
        check("token-convention-legacy", SharedTokenConvention.legacyService == "com.ev.evie.shell"
            && SharedTokenConvention.legacyAccount == "device_bearer")
        check("token-group-dotted", SharedTokenConvention.resolveAccessGroup(prefix: "ABC123D4EF.") == "ABC123D4EF.com.ev.ios")
        check("token-group-undotted", SharedTokenConvention.resolveAccessGroup(prefix: "ABC123D4EF") == "ABC123D4EF.com.ev.ios")
        check("token-group-nil", SharedTokenConvention.resolveAccessGroup(prefix: nil) == nil)
        check("token-group-blank", SharedTokenConvention.resolveAccessGroup(prefix: "  ") == nil)
        check("token-shared-slot", SharedTokenStore.shared(accessGroupPrefix: "ABC123D4EF.").accessGroup == "ABC123D4EF.com.ev.ios"
            && SharedTokenStore.shared(accessGroupPrefix: nil).accessGroup == nil)
        do {
            // Live roundtrip on a random service (mirrors EVClientCheck): proves
            // the save/load/delete path without touching any real token slot.
            let probe = SharedTokenStore(service: "ev.brokercheck.\(UUID().uuidString)", account: "probe")
            let saved = probe.save(token: "probe-token")
            let loaded = probe.load()
            probe.delete()
            check("token-roundtrip", saved && loaded == "probe-token" && probe.load() == nil)
        }

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
    expect("missing-none", EvieActionPlanner.missingPermissions(for: "start_timer", granted: []).isEmpty)
    expect("interpreted-local", EvieActionPlanner.plan(for: "interpreted_capture", grantedPermissions: all).kind == .local)
    expect("captured-text-local", EvieActionPlanner.plan(for: "captured_text", grantedPermissions: all).kind == .local)
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

// Mesh presence — iPhone-only, backward compat: dynamic executor routing self-test.
func evieMeshSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL mesh: \(name)") }
    }
    let foreground = EvieDevicePresence(deviceID: "a", cameraRank: 0, foreground: true, reachableViaTailscale: true, lastSeenSecondsAgo: 0)
    let background = EvieDevicePresence(deviceID: "b", cameraRank: 0, foreground: false, reachableViaTailscale: true, lastSeenSecondsAgo: 0)
    expect("foreground-wins", EvieMeshRouter.bestExecutor(for: "haptic", among: [background, foreground])?.deviceID == "a")
    let offline = EvieDevicePresence(deviceID: "c", reachableViaTailscale: false)
    expect("offline-never-wins", EvieMeshRouter.bestExecutor(for: "haptic", among: [offline]) == nil)
    let gated = EvieDevicePresence(deviceID: "d", reachableViaTailscale: true, grantedPermissions: [])
    expect("permission-gates", EvieMeshRouter.bestExecutor(for: "contacts_snapshot", among: [gated]) == nil)
    let stale = EvieDevicePresence(deviceID: "e", cameraRank: 0, foreground: true, reachableViaTailscale: true, lastSeenSecondsAgo: 999)
    expect("fresh-beats-stale", EvieMeshRouter.bestExecutor(for: "haptic", among: [stale, foreground])?.deviceID == "a")
    expect("heartbeat-keys", (foreground.heartbeat["device_id"] as? String) == "a")
    return ok
}

// Offline sync — iPhone-only, backward compat: scheduler self-test.
func evieSyncSchedulerSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL sync: \(name)") }
    }
    expect("fresh-sends", EvieSyncScheduler.verdict(for: .init(idempotencyKey: "k1", kind: "siri_capture")).0 == .sendNow)
    expect("backoff-grows", EvieSyncScheduler.backoffSeconds(attempts: 3) > EvieSyncScheduler.backoffSeconds(attempts: 1))
    expect("backoff-caps", EvieSyncScheduler.backoffSeconds(attempts: 99) == 256)
    expect("quarantine-max", EvieSyncScheduler.verdict(for: .init(idempotencyKey: "k2", kind: "share_capture", attempts: 8)).0 == .quarantine)
    expect("drop-unauth", EvieSyncScheduler.verdict(for: .init(idempotencyKey: "k3", kind: "siri_capture", attempts: 1, lastError: "UNAUTHENTICATED")).0 == .drop)
    let ordered = EvieSyncScheduler.order([.init(idempotencyKey: "t", kind: "telemetry"), .init(idempotencyKey: "s", kind: "siri_capture")])
    expect("capture-first", ordered.first?.kind == "siri_capture")
    return ok
}

// Capture interpreter — iPhone-only, backward compat: intent routing self-test.
func evieCaptureInterpreterSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL capture: \(name)") }
    }
    expect("plain-note", EvieCaptureInterpreter.interpret("Buy oat milk").intent == .note)
    expect("remind-keyword", EvieCaptureInterpreter.interpret("Remind me to call mom").intent == .reminder)
    expect("timer-keyword", EvieCaptureInterpreter.interpret("Set a timer for 10 minutes").intent == .timer)
    expect("delay-minutes", EvieCaptureInterpreter.parseDelay("in 10 minutes") == 600)
    expect("delay-half-hour", EvieCaptureInterpreter.parseDelay("in half an hour") == 1800)
    expect("empty-note", EvieCaptureInterpreter.interpret("   ").intent == .note)
    return ok
}

// Vision policy — iPhone-only, backward compat: fitness-based throttling self-test.
func evieVisionPolicySelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL vision: \(name)") }
    }
    let pro = EvieVisionPlanner.plan(cameraRank: 0, batteryPercent: 90, lowPowerMode: false, foreground: true)
    let se = EvieVisionPlanner.plan(cameraRank: 10, batteryPercent: 90, lowPowerMode: false, foreground: true)
    expect("pro-denser-than-se", pro.frameIntervalSeconds < se.frameIntervalSeconds && pro.maxFrames > se.maxFrames)
    let saver = EvieVisionPlanner.plan(cameraRank: 0, batteryPercent: 10, lowPowerMode: false, foreground: true)
    expect("low-battery-throttles", saver.reason == "power_saver" && saver.maxFrames < pro.maxFrames)
    let bg = EvieVisionPlanner.plan(cameraRank: 0, batteryPercent: 90, lowPowerMode: false, foreground: false)
    expect("background-minimal", bg.reason == "background" && bg.maxFrames <= 2)
    return ok
}

// Health snapshot — iPhone-only, backward compat: honest-degradation self-test.
func evieHealthSnapshotSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL health: \(name)") }
    }
    let off = EvieHealthPlanner.unavailable(reason: "no_health_data_on_device")
    expect("unavailable-flag", (off.payload["available"] as? Bool) == false)
    expect("never-sends", (off.payload["sent_to_model"] as? Bool) == false)
    let denied = EvieHealthPlanner.permissionRequired()
    expect("denied-permission", (denied.payload["permission"] as? String) == "denied")
    let full = EvieHealthSnapshot(available: true, permission: "granted", steps24h: 8000, activeEnergyKcal24h: 320, sleepHours24h: 7.5, freshness: "fresh_24h")
    let snap = full.payload["snapshot"] as? [String: Any]
    expect("steps-present", (snap?["steps_24h"] as? Int) == 8000)
    expect("sleep-present", (snap?["sleep_hours_24h"] as? Double) == 7.5)
    return ok
}
func evieCallPlannerSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL call: \(name)") }
    }
    expect("callkit-preferred", EvieCallPlanner.plan(kind: "call", digits: "+1 555-0100", displayName: "Mom", callKitAvailable: true)?.path == .callKit)
    expect("tel-fallback", EvieCallPlanner.plan(kind: "call", digits: "5550100", displayName: "Mom", callKitAvailable: false)?.path == .telFallback)
    expect("facetime-path", EvieCallPlanner.plan(kind: "facetime", digits: "5550100", displayName: "Mom", callKitAvailable: true)?.path == .faceTimeAudio)
    expect("empty-digits-nil", EvieCallPlanner.plan(kind: "call", digits: "  ", displayName: "Mom", callKitAvailable: true) == nil)
    expect("digits-cleaned", EvieCallPlanner.plan(kind: "call", digits: "(555) 010-0", displayName: "M", callKitAvailable: false)?.digits == "5550100")
    return ok
}

// Alarm schedule — iPhone-only, backward compat: honest scheduling self-test.
func evieAlarmPlannerSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL alarm: \(name)") }
    }
    let cd = EvieAlarmPlanner.plan(label: "Tea", delaySeconds: 300, fireDate: nil)
    expect("countdown", cd.schedule == .countdown(seconds: 300))
    expect("honest-kind", cd.timerKind == "evie_notification")
    let future = Date().addingTimeInterval(3600)
    let wc = EvieAlarmPlanner.plan(label: "Wake", delaySeconds: nil, fireDate: future)
    expect("wallclock", wc.schedule == .wallClock(fireDate: future))
    let past = EvieAlarmPlanner.plan(label: "X", delaySeconds: nil, fireDate: Date().addingTimeInterval(-60))
    expect("past-unsupported", past.schedule == .unsupported(reason: "fire_date_in_past"))
    let none = EvieAlarmPlanner.plan(label: nil, delaySeconds: nil, fireDate: nil)
    expect("no-time-unsupported", none.schedule == .unsupported(reason: "no_time_specified"))
    expect("default-label", none.label == "Evie timer")
    return ok
}

// Poll cadence — iPhone-only, backward compat: adaptive interval self-test.
func eviePollPlannerSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL poll: \(name)") }
    }
    expect("foreground-fast", EviePollPlanner.intervalSeconds(foreground: true, lowPowerMode: false, reachableViaTailscale: true) == 15)
    expect("urgent-faster", EviePollPlanner.intervalSeconds(foreground: true, lowPowerMode: false, reachableViaTailscale: true, hasUrgentItems: true) == 10)
    expect("background-slow", EviePollPlanner.intervalSeconds(foreground: false, lowPowerMode: false, reachableViaTailscale: true) == 300)
    expect("lowpower-slowest", EviePollPlanner.intervalSeconds(foreground: false, lowPowerMode: true, reachableViaTailscale: true) == 900)
    expect("offline-pauses", EviePollPlanner.intervalSeconds(foreground: true, lowPowerMode: false, reachableViaTailscale: false) == nil)
    return ok
}

// Message channel — iPhone-only, backward compat: placement self-test.
func evieMessageChannelSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL msgchan: \(name)") }
    }
    expect("composer-default", EvieMessageChannelPlanner.plan(requestedChannel: nil, message: "hi", canSendText: true, whatsAppAvailable: true).channel == .messagesSheet)
    expect("whatsapp-requested", EvieMessageChannelPlanner.plan(requestedChannel: "whatsapp", message: "hi", canSendText: true, whatsAppAvailable: true).channel == .whatsAppLink)
    expect("whatsapp-fallback", EvieMessageChannelPlanner.plan(requestedChannel: "WhatsApp", message: "hi", canSendText: true, whatsAppAvailable: false).channel == .messagesSheet)
    expect("sms-last-resort", EvieMessageChannelPlanner.plan(requestedChannel: nil, message: "hi", canSendText: false, whatsAppAvailable: false).channel == .smsFallback)
    expect("empty-needs-composer", EvieMessageChannelPlanner.plan(requestedChannel: nil, message: "  ", canSendText: false, whatsAppAvailable: true).channel == .messagesSheet)
    expect("wa-link-shape", (EvieMessageChannelPlanner.whatsAppURL(digits: "(555) 0100", message: "hi") ?? "").hasPrefix("https://wa.me/5550100"))
    return ok
}

// Siri phrases — iPhone-only, backward compat: dynamic generation self-test.
func evieSiriPhrasesSelfTestCase() -> Bool {
    var ok = true
    func expect(_ name: String, _ cond: Bool) {
        if !cond { ok = false; print("  FAIL siri: \(name)") }
    }
    expect("fixed-intact", EvieSiriPhraseList.phrases(forAppID: "spotify").contains("Open Spotify"))
    let first = AppLaunchRegistry.entries[0]
    expect("dynamic-generated", EvieSiriPhraseList.dynamicPhrases(for: first).contains("Open \(first.displayName)"))
    expect("unknown-empty", EvieSiriPhraseList.phrases(forAppID: "no_such_app_xyz").isEmpty)
    expect("all-nonempty", !EvieSiriPhraseList.allPhrases().isEmpty)
    return ok
}
