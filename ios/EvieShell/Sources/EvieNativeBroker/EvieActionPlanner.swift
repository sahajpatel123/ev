import Foundation

/// iPhone-only (EAC78): pure capability planner for the native shell.
///
/// Given a requested capability and the phone's granted permissions, the
/// planner answers where the action executes:
///   - `.local`       — the native broker performs it on this iPhone
///   - `.systemUI`    — the broker hands off to Apple UI (calls/messages/share)
///   - `.serverRouted`— Home Station validates and routes it (tool dispatch)
///   - `.unavailable` — unknown or deliberately blocked
///
/// The server remains the authority (the gateway re-validates every routed
/// capability); this planner only decides the local execution path and which
/// permission to request first. Pure Foundation — compile-checked on macOS.
public enum EviePlanKind: String, Equatable, Sendable {
    case local
    case systemUI
    case serverRouted
    case unavailable
}

public struct EvieCapabilityPlan: Equatable, Sendable {
    public let capability: String
    public let kind: EviePlanKind
    /// Permission the phone must grant before the action can run, if any.
    public let permissionRequired: String?
    /// Honest reason when the plan is unavailable or degraded.
    public let reason: String?

    public init(
        capability: String,
        kind: EviePlanKind,
        permissionRequired: String? = nil,
        reason: String? = nil
    ) {
        self.capability = capability
        self.kind = kind
        self.permissionRequired = permissionRequired
        self.reason = reason
    }
}

public enum EvieActionPlanner {
    /// Native broker actions that never leave the phone.
    public static let localCapabilities: Set<String> = [
        "haptic", "clipboard_write", "notification_status", "permission_status",
        "pending_capture", "healthkit_snapshot", "calendar_snapshot", "contacts_snapshot",
    ]

    /// Actions that must surface Apple's own confirmation UI.
    public static let systemUICapabilities: Set<String> = [
        "send_message", "place_call", "facetime", "share", "open_app", "compose_message",
    ]

    /// Actions Home Station validates and routes (mirrors the server manifest;
    /// the server list is authoritative).
    public static let serverRoutedCapabilities: Set<String> = [
        "start_timer", "cancel_timer", "list_timers",
        "set_reminder", "cancel_reminder", "list_reminders",
        "get_weather", "calendar_read", "list_mail", "list_messages",
        "open_url", "computer_status", "list_apps", "activate_app",
        "resolve_contact", "home_act", "device.echo", "device.ping",
        "mac.notify", "mac.echo",
    ]

    /// Permission needed per capability (nil = no permission gate).
    public static func permission(for capability: String) -> String? {
        switch capability {
        case "send_message", "place_call", "facetime", "resolve_contact", "contacts_snapshot":
            return "contacts"
        case "healthkit_snapshot":
            return "health"
        case "pending_capture", "notification_status":
            return "notifications"
        case "calendar_read", "calendar_snapshot":
            return "calendar"
        case "set_reminder", "list_reminders", "cancel_reminder", "create_reminder":
            return "reminders"
        case "clipboard_write":
            return nil
        default:
            return nil
        }
    }

    /// The denied/undetermined permissions that gate a capability.
    public static func missingPermissions(
        for capability: String,
        granted: Set<String>
    ) -> [String] {
        guard let required = permission(for: capability) else { return [] }
        return granted.contains(required) ? [] : [required]
    }

    public static func plan(
        for capability: String,
        grantedPermissions: Set<String>
    ) -> EvieCapabilityPlan {
        let cap = capability.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !cap.isEmpty else {
            return EvieCapabilityPlan(capability: cap, kind: .unavailable, reason: "EMPTY_CAPABILITY")
        }
        if localCapabilities.contains(cap) {
            let missing = missingPermissions(for: cap, granted: grantedPermissions)
            return EvieCapabilityPlan(
                capability: cap,
                kind: .local,
                permissionRequired: missing.first,
                reason: missing.isEmpty ? nil : "PERMISSION_REQUIRED"
            )
        }
        if systemUICapabilities.contains(cap) {
            let missing = missingPermissions(for: cap, granted: grantedPermissions)
            return EvieCapabilityPlan(
                capability: cap,
                kind: .systemUI,
                permissionRequired: missing.first,
                reason: missing.isEmpty ? nil : "PERMISSION_REQUIRED"
            )
        }
        if serverRoutedCapabilities.contains(cap) {
            return EvieCapabilityPlan(capability: cap, kind: .serverRouted)
        }
        return EvieCapabilityPlan(
            capability: cap,
            kind: .unavailable,
            reason: "UNKNOWN_CAPABILITY"
        )
    }
}
