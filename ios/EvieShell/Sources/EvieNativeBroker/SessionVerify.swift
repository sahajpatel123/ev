import Foundation

/// Verify-not-trust for the web-to-native `bind_session` bridge.
///
/// The PWA hands the shell a `device_token` it obtained from pairing. The
/// shell must never persist a token the server rejects: a forged token would
/// otherwise overwrite the good one (lockout) or plant a foreign identity.
/// Verification is a cheap side-effect-free GET against the Home Station with
/// the candidate token; only the HTTP status matters.
///
/// Decision table (`nil` = no network answer):
/// - 200          -> store, report verified
/// - 401 / 403    -> delete any stored token, report UNAUTHENTICATED
/// - anything else -> keep the token but report unverified, so a Tailnet
///   blip during PWA boot does not brick the shell; the PWA re-sends
///   `bind_session` on every boot, which re-verifies.
public enum BindSessionVerifier {
    /// Side-effect-free, device-authed, contract-locked verify target.
    public static let verifyPath = "/v1/voice/hands-free/status"

    public enum Decision: Equatable, Sendable {
        case verifiedStore
        case rejectedDelete
        case unverifiedKeep
    }

    public static func decide(statusCode: Int?) -> Decision {
        switch statusCode {
        case 200:
            return .verifiedStore
        case 401, 403:
            return .rejectedDelete
        default:
            return .unverifiedKeep
        }
    }
}
