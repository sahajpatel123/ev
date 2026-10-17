// Stage 2 (foreground only) — relay-vs-direct route selector (see docs/IPHONE_FUTURE_RELAY.md).
// New file; default is direct (relay opt-in via EvieRelayConfig.enabled).

import Foundation
import Network

// MARK: - Config

/// Relay opt-in flag. Default direct: `enabled == false` always routes direct.
public struct EvieRelayConfig: Equatable, Sendable {
    public var enabled: Bool
    /// Known Mac tailnet origin, e.g. `https://mac.tailnet.ts.net` (already-stored api origin).
    public var directBaseURL: URL
    /// Relay listener port suggestion (doc: 9411).
    public var relayPort: UInt16
    /// p95 margin (ms) relay must beat direct by (rule 1).
    public var latencyMarginMs: Double
    /// Consecutive relay failures before backoff (rule 5).
    public var maxConsecutiveFailures: Int

    public init(
        enabled: Bool = false,
        directBaseURL: URL,
        relayPort: UInt16 = 9411,
        latencyMarginMs: Double = 50,
        maxConsecutiveFailures: Int = 2
    ) {
        self.enabled = enabled
        self.directBaseURL = directBaseURL
        self.relayPort = relayPort
        self.latencyMarginMs = latencyMarginMs
        self.maxConsecutiveFailures = maxConsecutiveFailures
    }
}

// MARK: - Types

/// Relay candidate from Bonjour TXT or Tailscale relay/info.
public struct EvieRelayCandidate: Equatable, Sendable {
    public var baseURL: URL
    public var fingerprint: String
    public var macID: String

    public init(baseURL: URL, fingerprint: String, macID: String) {
        self.baseURL = baseURL
        self.fingerprint = fingerprint
        self.macID = macID
    }
}

/// Relay health payload (`GET /v1/device-gateway/relay/health`).
public struct EvieRelayHealth: Equatable, Sendable {
    public var ok: Bool
    public var backendReachable: Bool
    public var fingerprint: String

    public init(ok: Bool, backendReachable: Bool, fingerprint: String) {
        self.ok = ok
        self.backendReachable = backendReachable
        self.fingerprint = fingerprint
    }
}

/// Route evaluation inputs (latency p95 in ms over ~3 probes).
public struct EvieRouteSample: Equatable, Sendable {
    public var candidate: EvieRelayCandidate?
    public var health: EvieRelayHealth?
    public var relayP95Ms: Double?
    public var directP95Ms: Double?
    public var tailnetUp: Bool
    public var consecutiveRelayFailures: Int
    public var voiceCallActive: Bool
    public var syncInFlight: Bool
    public var probeTimedOut: Bool
    public var currentRoute: EvieRoute

    public init(
        candidate: EvieRelayCandidate? = nil,
        health: EvieRelayHealth? = nil,
        relayP95Ms: Double? = nil,
        directP95Ms: Double? = nil,
        tailnetUp: Bool = false,
        consecutiveRelayFailures: Int = 0,
        voiceCallActive: Bool = false,
        syncInFlight: Bool = false,
        probeTimedOut: Bool = false,
        currentRoute: EvieRoute = .direct
    ) {
        self.candidate = candidate
        self.health = health
        self.relayP95Ms = relayP95Ms
        self.directP95Ms = directP95Ms
        self.tailnetUp = tailnetUp
        self.consecutiveRelayFailures = consecutiveRelayFailures
        self.voiceCallActive = voiceCallActive
        self.syncInFlight = syncInFlight
        self.probeTimedOut = probeTimedOut
        self.currentRoute = currentRoute
    }
}

public enum EvieRoute: Equatable, Sendable {
    case direct
    case relay(EvieRelayCandidate)
}

/// Decision outcome with the fired table rule number.
public struct EvieRouteDecision: Equatable, Sendable {
    public var route: EvieRoute
    public var rule: Int
    /// Rule 3 fires this once per mismatch (caller dedupes presentation).
    public var spoofAlert: Bool

    public init(route: EvieRoute, rule: Int, spoofAlert: Bool = false) {
        self.route = route
        self.rule = rule
        self.spoofAlert = spoofAlert
    }
}

// MARK: - Decision table (pure, testable)

public enum EvieRelayRoute {
    /// Implements the §5 decision table. Default: direct. Stickiness (rules 6–7)
    /// is checked before any switch; rules 2/3/5 force back to direct.
    public static func decide(config: EvieRelayConfig, sample: EvieRouteSample, trustedFingerprint: String?) -> EvieRouteDecision {
        guard config.enabled else { return EvieRouteDecision(route: .direct, rule: 4) }

        // Rule 5 (backoff trigger): flaky relay -> direct.
        if sample.probeTimedOut || sample.consecutiveRelayFailures >= config.maxConsecutiveFailures {
            return EvieRouteDecision(route: .direct, rule: 5)
        }

        guard let candidate = sample.candidate, let health = sample.health else {
            // Rule 4: no candidate (Bonjour found nothing, tailnet down).
            return EvieRouteDecision(route: .direct, rule: 4)
        }

        // Rule 3: pin mismatch -> direct + alert once. Never fall forward.
        if let trusted = trustedFingerprint, !trusted.isEmpty,
           health.fingerprint != trusted || candidate.fingerprint != trusted {
            return EvieRouteDecision(route: .direct, rule: 3, spoofAlert: true)
        }

        // Rule 2: Mac up, backend down -> direct.
        guard health.ok, health.backendReachable else {
            return EvieRouteDecision(route: .direct, rule: 2)
        }

        // Rules 6–7: stick — no mid-call / mid-sync switch.
        if sample.voiceCallActive { return EvieRouteDecision(route: sample.currentRoute, rule: 6) }
        if sample.syncInFlight { return EvieRouteDecision(route: sample.currentRoute, rule: 7) }

        // Rule 1: relay measurably faster.
        if let relay = sample.relayP95Ms, let direct = sample.directP95Ms,
           relay < direct - config.latencyMarginMs {
            return EvieRouteDecision(route: .relay(candidate), rule: 1)
        }

        return EvieRouteDecision(route: .direct, rule: 4)
    }

    /// Base URL for gateway calls: relay Mac URL or direct backend URL.
    public static func baseURL(config: EvieRelayConfig, decision: EvieRouteDecision) -> URL {
        switch decision.route {
        case .direct: return config.directBaseURL
        case .relay(let candidate): return candidate.baseURL
        }
    }
}

// MARK: - Discovery stub (Stage 2 foreground-only)

/// Short-lived Bonjour browse + Tailscale relay/info lookup.
/// Stub: real NWBrowser/URLSession wiring lands with slice 5 driver; this
/// establishes the call shape and Wi-Fi-only browse gating.
public final class EvieRelayDiscovery: Sendable {
    public let serviceType = "_evie-relay._tcp"
    private let config: EvieRelayConfig

    public init(config: EvieRelayConfig) {
        self.config = config
    }

    /// Returns a relay candidate, or nil (rule 4). Browse runs only on Wi-Fi.
    public func discover(
        usesWiFi: Bool,
        tailnetUp: Bool,
        bonjourBrowse: () -> EvieRelayCandidate?,
        tailnetInfo: () -> EvieRelayCandidate?
    ) -> EvieRelayCandidate? {
        guard config.enabled else { return nil }
        if usesWiFi, let found = bonjourBrowse() { return found }
        if tailnetUp { return tailnetInfo() }
        return nil
    }

    /// Tailscale relay/info URL for port/pin learning (authenticated call).
    public func relayInfoURL() -> URL {
        config.directBaseURL.appendingPathComponent("v1/device-gateway/relay/info")
    }

    /// Relay health URL for route scoring probes.
    public static func healthURL(candidate: EvieRelayCandidate) -> URL {
        candidate.baseURL.appendingPathComponent("v1/device-gateway/relay/health")
    }
}
