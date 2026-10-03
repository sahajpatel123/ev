import Foundation

/// iPhone-only: dynamic message-channel planner.
///
/// Picks how a "message X" request should go out — native Messages sheet,
/// WhatsApp link, or plain `sms:` fallback — from the resolved contact, the
/// message shape, and runtime capability, instead of hardcoding one path.
/// Silent send stays refused everywhere; every path ends in Apple UI.
/// Pure Foundation so the package build verifies it.
public enum EvieMessageChannel: String, Equatable, Sendable {
    case messagesSheet
    case whatsAppLink
    case smsFallback
}

public struct EvieMessagePlan: Equatable, Sendable {
    public var channel: EvieMessageChannel
    public var reason: String

    public init(channel: EvieMessageChannel, reason: String) {
        self.channel = channel
        self.reason = reason
    }
}

public enum EvieMessageChannelPlanner {
    public static func plan(
        requestedChannel: String?,
        message: String,
        canSendText: Bool,
        whatsAppAvailable: Bool
    ) -> EvieMessagePlan {
        let ask = (requestedChannel ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if ask.contains("whatsapp") {
            if whatsAppAvailable {
                return .init(channel: .whatsAppLink, reason: "whatsapp_requested")
            }
            if canSendText {
                return .init(channel: .messagesSheet, reason: "whatsapp_unavailable_fallback")
            }
            return .init(channel: .smsFallback, reason: "whatsapp_unavailable_sms")
        }
        if message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .init(channel: .messagesSheet, reason: "empty_body_needs_composer")
        }
        if canSendText {
            return .init(channel: .messagesSheet, reason: "native_composer")
        }
        return .init(channel: .smsFallback, reason: "composer_unavailable")
    }

    public static func whatsAppURL(digits: String, message: String) -> String? {
        let clean = digits.filter { $0.isNumber || $0 == "+" }
        guard !clean.isEmpty else { return nil }
        var parts = URLComponents()
        parts.scheme = "https"
        parts.host = "wa.me"
        parts.path = "/\(clean)"
        if !message.isEmpty {
            parts.queryItems = [URLQueryItem(name: "text", value: message)]
        }
        return parts.url?.absoluteString
    }
}
