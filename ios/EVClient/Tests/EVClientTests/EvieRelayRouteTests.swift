import Foundation
import Testing

@testable import EVClient

/// Decision table §5: rules 1–7. Pure logic, no I/O.
@Suite("Relay route decision table")
struct EvieRelayRouteTests {
    private func config() -> EvieRelayConfig {
        EvieRelayConfig(enabled: true, directBaseURL: URL(string: "https://api.example.com")!)
    }
    private func cand(_ fp: String = "fp-good") -> EvieRelayCandidate {
        EvieRelayCandidate(baseURL: URL(string: "http://mac:9411")!, fingerprint: fp, macID: "mac1")
    }
    private func health(ok: Bool = true, backend: Bool = true, fp: String = "fp-good") -> EvieRelayHealth {
        EvieRelayHealth(ok: ok, backendReachable: backend, fingerprint: fp)
    }

    @Test("rule 1: relay wins when faster by margin")
    func rule1() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 40, directP95Ms: 120), trustedFingerprint: "fp-good")
        #expect(d.rule == 1)
        #expect(d.route == .relay(cand()))
    }

    @Test("rule 1: direct when margin not met")
    func rule1MarginMiss() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 100, directP95Ms: 120), trustedFingerprint: "fp-good")
        #expect(d.rule == 4 && d.route == .direct)
    }

    @Test("rule 2: backend down forces direct")
    func rule2() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(backend: false), relayP95Ms: 10, directP95Ms: 500), trustedFingerprint: "fp-good")
        #expect(d.rule == 2 && d.route == .direct)
    }

    @Test("rule 3: pin mismatch forces direct + spoof alert")
    func rule3() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand("fp-evil"), health: health(fp: "fp-evil"), relayP95Ms: 10, directP95Ms: 500), trustedFingerprint: "fp-good")
        #expect(d.rule == 3 && d.route == .direct && d.spoofAlert)
    }

    @Test("rule 4: disabled config routes direct")
    func rule4Disabled() {
        var c = config(); c.enabled = false
        let d = EvieRelayRoute.decide(config: c, sample: EvieRouteSample(), trustedFingerprint: nil)
        #expect(d.rule == 4 && d.route == .direct)
    }

    @Test("rule 4: no candidate routes direct")
    func rule4NoCandidate() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(), trustedFingerprint: nil)
        #expect(d.rule == 4 && d.route == .direct)
    }

    @Test("rule 5: consecutive failures force direct")
    func rule5() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 10, directP95Ms: 500, consecutiveRelayFailures: 2), trustedFingerprint: "fp-good")
        #expect(d.rule == 5 && d.route == .direct)
    }

    @Test("rule 5: probe timeout forces direct")
    func rule5Timeout() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), probeTimedOut: true), trustedFingerprint: "fp-good")
        #expect(d.rule == 5 && d.route == .direct)
    }

    @Test("rule 6: voice call sticks to current route")
    func rule6() {
        let relayRoute = EvieRoute.relay(cand())
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 500, directP95Ms: 10,
            voiceCallActive: true, currentRoute: relayRoute), trustedFingerprint: "fp-good")
        #expect(d.rule == 6 && d.route == relayRoute)
    }

    @Test("rule 7: in-flight sync sticks to current route")
    func rule7() {
        let relayRoute = EvieRoute.relay(cand())
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 500, directP95Ms: 10,
            syncInFlight: true, currentRoute: relayRoute), trustedFingerprint: "fp-good")
        #expect(d.rule == 7 && d.route == relayRoute)
    }

    @Test("precedence: unreachable beats active voice call")
    func precedenceUnreachableOverVoice() {
        let relayRoute = EvieRoute.relay(cand())
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(backend: false), relayP95Ms: 10, directP95Ms: 500,
            voiceCallActive: true, currentRoute: relayRoute), trustedFingerprint: "fp-good")
        #expect(d.rule == 2 && d.route == .direct)
    }

    @Test("precedence: probe timeout beats in-flight sync")
    func precedenceTimeoutOverSync() {
        let relayRoute = EvieRoute.relay(cand())
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), probeTimedOut: true,
            syncInFlight: true, currentRoute: relayRoute), trustedFingerprint: "fp-good")
        #expect(d.rule == 5 && d.route == .direct)
    }

    @Test("precedence: pin mismatch beats active voice call")
    func precedenceMismatchOverVoice() {
        let relayRoute = EvieRoute.relay(cand("fp-evil"))
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand("fp-evil"), health: health(fp: "fp-evil"), relayP95Ms: 10, directP95Ms: 500,
            voiceCallActive: true, currentRoute: relayRoute), trustedFingerprint: "fp-good")
        #expect(d.rule == 3 && d.route == .direct && d.spoofAlert)
    }

    @Test("rule 2: unhealthy relay forces direct")
    func rule2Unhealthy() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(ok: false), relayP95Ms: 10, directP95Ms: 500), trustedFingerprint: "fp-good")
        #expect(d.rule == 2 && d.route == .direct)
    }

    @Test("rule 6: voice call sticks to direct")
    func rule6StickDirect() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 10, directP95Ms: 500,
            voiceCallActive: true, currentRoute: .direct), trustedFingerprint: "fp-good")
        #expect(d.rule == 6 && d.route == .direct)
    }

    @Test("rule 7: in-flight sync sticks to direct")
    func rule7StickDirect() {
        let d = EvieRelayRoute.decide(config: config(), sample: EvieRouteSample(
            candidate: cand(), health: health(), relayP95Ms: 10, directP95Ms: 500,
            syncInFlight: true, currentRoute: .direct), trustedFingerprint: "fp-good")
        #expect(d.rule == 7 && d.route == .direct)
    }
}
