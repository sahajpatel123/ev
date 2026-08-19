import Foundation

/// Connection state for the Mac-owned live-runtime facts.
public enum LiveRuntimeConnectionState: String, Sendable, Equatable {
    case idle
    case connecting
    case connected
    case reconnecting
    case muted
    case stopped
    case offline
}

/// The least-sensitive useful proof of a live function call. Argument values
/// are intentionally not retained; keys are enough to prove the call shape
/// without putting owner data into a diagnostics surface.
public struct LiveRuntimeToolCall: Sendable, Equatable {
    public let name: String
    public let callID: String?
    public let argumentKeys: [String]
    public let observedAt: String?

    public init(
        name: String,
        callID: String? = nil,
        argumentKeys: [String] = [],
        observedAt: String? = nil
    ) {
        self.name = name
        self.callID = callID
        self.argumentKeys = argumentKeys
        self.observedAt = observedAt
    }

    public var displayText: String {
        var value = name
        if !argumentKeys.isEmpty {
            value += "(keys: \(argumentKeys.joined(separator: ", ")))"
        }
        if let callID, !callID.isEmpty {
            value += " · call \(callID)"
        }
        return value
    }
}

/// The latest provider/tool result shown to the owner. ``verified`` means
/// the backend's result contract marked it as successful; the Mac never
/// promotes a failed or degraded card into evidence.
public struct LiveRuntimeToolResult: Sendable, Equatable {
    public let name: String
    public let success: Bool
    public let verified: Bool
    public let summary: String
    public let error: String?
    public let observedAt: String?

    public init(
        name: String,
        success: Bool,
        verified: Bool,
        summary: String,
        error: String? = nil,
        observedAt: String? = nil
    ) {
        self.name = name
        self.success = success
        self.verified = verified
        self.summary = summary
        self.error = error
        self.observedAt = observedAt
    }

    public var displayText: String {
        let state = verified ? "verified" : (success ? "successful" : "failed")
        if let error, !error.isEmpty {
            return "\(name) · \(state): \(error)"
        }
        return "\(name) · \(state): \(summary)"
    }
}

/// Evidence attached to the latest successful live tool result.
public struct LiveRuntimeEvidence: Sendable, Equatable {
    public let source: String
    public let timestamp: String?
    public let summary: String

    public init(source: String, timestamp: String? = nil, summary: String = "") {
        self.source = source
        self.timestamp = timestamp
        self.summary = summary
    }

    public var displayText: String {
        var value = source
        if let timestamp, !timestamp.isEmpty {
            value += " · \(timestamp)"
        }
        if !summary.isEmpty {
            value += ": \(summary)"
        }
        return value
    }
}

/// Client-side facts for diagnosing the exact Mac live-runtime path.
///
/// This is deliberately a presentation/evidence model. It does not decide
/// which capabilities or tools are allowed, and it does not mirror secrets,
/// tool argument values, or provider credentials.
public struct LiveRuntimeDiagnostics: Sendable, Equatable {
    public var connectionState: LiveRuntimeConnectionState = .idle
    public var backendURL: String?
    public var backendURLSource: String?
    public var backendStatus: String?
    public var backendVersion: String?
    public var backendEnvironment: String?
    public var backendPID: Int?
    public var backendStartedAt: String?
    public var backendSourceFingerprint: String?
    public var provider: String?
    public var model: String?
    public var sessionID: String?
    public var deviceID: String?
    public var advertisedTools: [String] = []
    public var providerAcknowledgedTools: [String] = []
    public var providerSessionReady = false
    public var capabilityErrors: [String] = []
    public var lastToolCall: LiveRuntimeToolCall?
    public var lastToolResult: LiveRuntimeToolResult?
    public var lastEvidence: LiveRuntimeEvidence?
    public var connectionAttempts = 0
    public var reconnectCount = 0
    public var lastConnectedAt: String?
    public var lastDisconnectedAt: String?
    public var lastDisconnectReason: String?

    public init() {}

    public mutating func setBackend(url: URL, source: String?) {
        if var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.user = nil
            components.password = nil
            components.query = nil
            components.fragment = nil
            backendURL = components.string
        } else {
            backendURL = url.absoluteString
        }
        backendURLSource = source
    }

    public mutating func beginConnectionAttempt(
        backendURL url: URL,
        backendSource: String?,
        deviceID: String
    ) {
        setBackend(url: url, source: backendSource)
        self.deviceID = deviceID
        connectionAttempts += 1
        if connectionAttempts > 1 {
            reconnectCount += 1
        }
        provider = nil
        model = nil
        advertisedTools = []
        providerAcknowledgedTools = []
        providerSessionReady = false
        capabilityErrors = []
        connectionState = .connecting
    }

    public mutating func sessionOpened(sessionID: String, deviceID: String) {
        self.sessionID = sessionID
        self.deviceID = deviceID
        connectionState = .connecting
    }

    public mutating func connected() {
        connectionState = .connected
        lastConnectedAt = Self.timestamp()
        lastDisconnectReason = nil
    }

    public mutating func muted() {
        connectionState = .muted
    }

    public mutating func disconnected(reason: String, willReconnect: Bool) {
        connectionState = willReconnect ? .reconnecting : .offline
        lastDisconnectedAt = Self.timestamp()
        lastDisconnectReason = reason
    }

    public mutating func stopped() {
        connectionState = .stopped
    }

    public mutating func updateRuntime(
        provider: String?,
        model: String?,
        advertisedTools: [String],
        providerAcknowledgedTools: [String],
        providerSessionReady: Bool,
        capabilityErrors: [String]
    ) {
        if let provider, !provider.isEmpty { self.provider = provider }
        if let model, !model.isEmpty { self.model = model }
        if !advertisedTools.isEmpty || providerSessionReady {
            self.advertisedTools = Self.unique(advertisedTools)
        }
        if !providerAcknowledgedTools.isEmpty || providerSessionReady {
            self.providerAcknowledgedTools = Self.unique(providerAcknowledgedTools)
        }
        self.providerSessionReady = providerSessionReady
        self.capabilityErrors = Self.unique(capabilityErrors)
    }

    public mutating func addCapabilityError(_ error: String) {
        guard !error.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        capabilityErrors = Self.unique(capabilityErrors + [error])
    }

    public mutating func recordToolCall(_ call: LiveRuntimeToolCall) {
        lastToolCall = call
    }

    public mutating func recordToolResult(_ result: LiveRuntimeToolResult) {
        lastToolResult = result
    }

    public mutating func recordEvidence(_ evidence: LiveRuntimeEvidence) {
        lastEvidence = evidence
    }

    /// Compact, owner-readable diagnostics suitable for a menu/panel/log.
    /// Missing provider facts remain explicitly visible as ``not reported``.
    public var displayText: String {
        displayLines.joined(separator: "\n")
    }

    public var displayLines: [String] {
        let backend = backendURL ?? "not reported"
        let source = backendURLSource.map { " [\($0)]" } ?? ""
        let providerText = provider ?? "not reported"
        let modelText = model ?? "not reported by backend"
        let sessionText = sessionID ?? "not reported"
        let deviceText = deviceID ?? "not reported"
        let advertised = advertisedTools.isEmpty ? "none" : advertisedTools.joined(separator: ", ")
        let acknowledged = providerAcknowledgedTools.isEmpty
            ? "none"
            : providerAcknowledgedTools.joined(separator: ", ")
        let errors = capabilityErrors.isEmpty ? "none" : capabilityErrors.joined(separator: " | ")
        let process = backendPID.map(String.init) ?? "not reported"
        let version = backendVersion ?? "not reported"
        let started = backendStartedAt ?? "not reported"
        let fingerprint = backendSourceFingerprint ?? "not reported"
        return [
            "backend: \(backend)\(source)",
            "process: \(process) · version: \(version) · started: \(started) · fingerprint: \(fingerprint)",
            "provider: \(providerText) · model: \(modelText)",
            "session: \(sessionText) · device: \(deviceText)",
            "tools advertised: \(advertised)",
            "tools acknowledged: \(acknowledged) · provider session ready: \(providerSessionReady)",
            "capability errors: \(errors)",
            "last tool call: \(lastToolCall?.displayText ?? "none")",
            "last tool result: \(lastToolResult?.displayText ?? "none")",
            "last evidence: \(lastEvidence?.displayText ?? "none")",
            "connection: \(connectionState.rawValue) · attempts: \(connectionAttempts) · reconnects: \(reconnectCount)",
            "last disconnect: \(lastDisconnectReason ?? "none")",
        ]
    }

    private static func unique(_ values: [String]) -> [String] {
        var seen = Set<String>()
        return values.compactMap { raw in
            let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty, seen.insert(value).inserted else { return nil }
            return value
        }
    }

    private static func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}
