// Cycle EAC-80 — iPhone launch readiness.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// EV has different prerequisites for text, live voice, and Look.  This
/// framework-free model keeps unknown permissions and unverified trust visible
/// instead of presenting a partially working phone as ready.
import Foundation

public struct EvieLaunchReadiness: Equatable, Sendable {
    public enum Capability: String, Sendable, CaseIterable {
        case text
        case liveVoice = "live_voice"
        case look
    }

    public let networkAvailable: Bool?
    public let microphoneAuthorized: Bool?
    public let voiceEnrolled: Bool?
    public let cameraAuthorized: Bool?
    public let serverTrustConfirmed: Bool
    public let trustWasServerReported: Bool

    public init(
        networkAvailable: Bool?,
        microphoneAuthorized: Bool? = nil,
        voiceEnrolled: Bool? = nil,
        cameraAuthorized: Bool? = nil,
        serverTrustConfirmed: Bool = false,
        trustWasServerReported: Bool = false
    ) {
        self.networkAvailable = networkAvailable
        self.microphoneAuthorized = microphoneAuthorized
        self.voiceEnrolled = voiceEnrolled
        self.cameraAuthorized = cameraAuthorized
        self.serverTrustConfirmed = serverTrustConfirmed
        self.trustWasServerReported = trustWasServerReported
    }

    public var isServerTrusted: Bool {
        trustWasServerReported && serverTrustConfirmed
    }

    public func isReady(for capability: Capability) -> Bool {
        guard networkAvailable == true else { return false }
        switch capability {
        case .text:
            return true
        case .liveVoice:
            return isServerTrusted && microphoneAuthorized == true && voiceEnrolled == true
        case .look:
            return isServerTrusted && cameraAuthorized == true
        }
    }

    public func nextStep(for capability: Capability) -> String? {
        if networkAvailable != true {
            return networkAvailable == false ? "Reconnect this iPhone to the network" : "Checking network connection"
        }
        switch capability {
        case .text:
            return nil
        case .liveVoice:
            if !isServerTrusted { return "Approve this phone from Home Station" }
            if microphoneAuthorized != true {
                return microphoneAuthorized == false ? "Allow microphone access" : "Checking microphone access"
            }
            if voiceEnrolled != true {
                return voiceEnrolled == false ? "Enroll your voiceprint" : "Checking voice enrollment"
            }
        case .look:
            if !isServerTrusted { return "Approve this phone from Home Station" }
            if cameraAuthorized != true {
                return cameraAuthorized == false ? "Allow camera access" : "Checking camera access"
            }
        }
        return nil
    }

    public var displayLine: String {
        if isReady(for: .liveVoice) && isReady(for: .look) {
            return "EV ready · voice + Look"
        }
        if isReady(for: .liveVoice) {
            return "EV ready · voice"
        }
        if isReady(for: .text) {
            return "EV ready · text"
        }
        return nextStep(for: .text) ?? "EV setup in progress"
    }
}
