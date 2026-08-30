import Foundation

public enum BrokerVersion {
    public static let version = "1.0.0"
    public static let protocolVersion = 1
}

public struct NativeReceipt: Sendable, Equatable {
    public var actionID: String
    public var accepted: Bool
    public var executed: Bool
    public var verified: Bool
    public var systemUIPresented: Bool
    public var systemConfirmationRequired: Bool
    public var result: String
    public var failure: String?

    public init(
        actionID: String,
        accepted: Bool,
        executed: Bool,
        verified: Bool,
        systemUIPresented: Bool,
        systemConfirmationRequired: Bool,
        result: String,
        failure: String? = nil
    ) {
        self.actionID = actionID
        self.accepted = accepted
        self.executed = executed
        self.verified = verified
        self.systemUIPresented = systemUIPresented
        self.systemConfirmationRequired = systemConfirmationRequired
        self.result = result
        self.failure = failure
    }

    public var jsonObject: [String: Any] {
        var payload: [String: Any] = [
            "action_id": actionID,
            "accepted": accepted,
            "executed": executed,
            "verified": verified,
            "system_ui_presented": systemUIPresented,
            "system_confirmation_required": systemConfirmationRequired,
            "result": result,
        ]
        if let failure {
            payload["failure"] = failure
        }
        return payload
    }
}

public enum HapticEvent: String, Sendable {
    case selection
    case voiceStarted = "voice_started"
    case voiceStopped = "voice_stopped"
    case actionUnderstood = "action_understood"
    case confirmationRequested = "confirmation_requested"
    case confirmationAccepted = "confirmation_accepted"
    case actionSuccess = "action_success"
    case actionFailure = "action_failure"
    case visionCapture = "vision_capture"
    case deviceSwitch = "device_switch"
}

public struct NativeBridgeRequest: Sendable {
    public let type: String
    public let actionID: String?
    public let event: String?
    public let requestID: String?

    public static let allowedTypes: Set<String> = [
        "haptic",
        "capabilities",
        "execute",
        "requestPermission",
        "permissionStatus",
        "bind_session",
    ]

    public static func parse(_ body: [String: Any]) -> NativeBridgeRequest? {
        let type = (body["type"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if type.isEmpty { return nil }
        let banned = ["invokeSelector", "executeNative", "callFramework", "eval", "run_shortcut"]
        if banned.contains(type) { return nil }
        guard allowedTypes.contains(type) else { return nil }
        return NativeBridgeRequest(
            type: type,
            actionID: body["action_id"] as? String,
            event: body["event"] as? String,
            requestID: body["request_id"] as? String
        )
    }
}
