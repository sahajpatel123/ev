import EvieNativeBroker
import Foundation
import Security

enum DeviceAuth {
    private static var shared: SharedTokenStore {
        SharedTokenStore.shared(
            accessGroupPrefix: SharedTokenConvention.appIdentifierPrefix()
        )
    }

    static func token() -> String? {
        if let current = shared.load() {
            return current
        }
        // One-way migration: a token stored by the pre-unification shell
        // moves into the shared slot on first sight, then the legacy slot
        // is removed so the two can never disagree again.
        let legacy = SharedTokenStore.legacyShell()
        guard let migrated = legacy.load() else { return nil }
        shared.save(token: migrated)
        legacy.delete()
        return migrated
    }

    static func store(_ token: String) {
        shared.save(token: token)
        SharedTokenStore.legacyShell().delete()
    }

    static func delete() {
        shared.delete()
        SharedTokenStore.legacyShell().delete()
    }
}

enum GatewayClient {
    /// Authenticated GET returning only the HTTP status (nil = no answer).
    /// Used to verify a candidate bearer before the shell trusts it.
    static func getStatus(origin: String, path: String, token: String) async -> Int? {
        guard let url = URL(string: origin.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + path) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            return (response as? HTTPURLResponse)?.statusCode
        } catch {
            return nil
        }
    }

    static func post(origin: String, path: String, token: String, body: [String: Any] = [:]) async -> [String: Any]? {
        await send(origin: origin, path: path, token: token, method: "POST", body: body)?.json
    }

    /// Status plus a JSON object. A non-2xx body is still returned so the
    /// caller can show the server's reason instead of a fake success.
    static func send(
        origin: String,
        path: String,
        token: String,
        method: String,
        body: [String: Any]? = nil
    ) async -> (status: Int, json: [String: Any])? {
        let trimmed = origin.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: trimmed + path) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let json = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            return (status, json)
        } catch {
            return nil
        }
    }
}

// Cycle 33 — iPhone-only, backward compat: device-auth trust-state display model.
// Display-only; DeviceAuth token storage unchanged.
enum EvieDeviceTrustState: String, Sendable, CaseIterable {
    case pairedSandbox = "PAIRED_SANDBOX"
    case trustedOwnerDevice = "TRUSTED_OWNER_DEVICE"
    case revoked = "REVOKED"

    var displayName: String {
        switch self {
        case .pairedSandbox: return "Paired Sandbox"
        case .trustedOwnerDevice: return "Trusted Owner Device"
        case .revoked: return "Revoked"
        }
    }

    var isUsable: Bool { self != .revoked }
}
