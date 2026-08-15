/// LIFE-access APIs shared by the macOS helper and the iOS app: permission
/// reports, contacts, messages/calls, and notification summaries.
///
/// Platform capabilities are guarded with `canImport` so the package still
/// builds on macOS, iOS, and watchOS with Command Line Tools alone.

import Foundation
import UserNotifications
#if canImport(Contacts)
import Contacts
#endif
#if canImport(UIKit)
import UIKit
#elseif os(macOS)
import AppKit
#endif
#if canImport(MessageUI)
import MessageUI
#endif
#if canImport(LocalAuthentication)
import LocalAuthentication
#endif

public enum EVLifeError: Error, Sendable, Equatable {
    case permissionDenied(String)
    case unavailable(String)
    case invalidArgument(String)
    case biometricRequired
}

public enum EVLifeBiometric {
    /// Face ID / Touch ID before place_call, lock home_act, or delegate_grant.
    /// Failure means the client must not send the request.
    public static func confirmLifeAction(reason: String) async -> Bool {
        #if canImport(LocalAuthentication)
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else {
            return false
        }
        return await withCheckedContinuation { continuation in
            context.evaluatePolicy(
                .deviceOwnerAuthenticationWithBiometrics,
                localizedReason: reason
            ) { success, _ in
                continuation.resume(returning: success)
            }
        }
        #else
        return false
        #endif
    }
}

// MARK: - Permission report → backend

public struct EVPermissionEntry: Codable, Sendable, Equatable {
    public let permission: String
    public let state: String
    public let detail: String?

    public init(permission: String, state: String, detail: String? = nil) {
        self.permission = permission
        self.state = state
        self.detail = detail
    }
}

public struct EVPermissionReport: Codable, Sendable, Equatable {
    public let platform: String
    public let deviceId: String?
    public let generatedAt: String
    public let permissions: [EVPermissionEntry]

    public init(
        platform: String,
        deviceId: String?,
        generatedAt: String = ISO8601DateFormatter().string(from: Date()),
        permissions: [EVPermissionEntry]
    ) {
        self.platform = platform
        self.deviceId = deviceId
        self.generatedAt = generatedAt
        self.permissions = permissions
    }
}

extension EVAPIClient {
    /// POST the device permission snapshot to
    /// `POST /v1/devices/{id}/permissions` (additive endpoint owned by
    /// Agent 14/Agent 1; the call fails gracefully until it lands).
    public func postPermissionReport(
        _ report: EVPermissionReport,
        deviceID: String
    ) async throws -> Bool {
        let body = try encode(report)
        let (status, _) = try await send(
            "/v1/devices/\(deviceID)/permissions",
            method: "POST",
            body: body,
            allowedStatuses: [200, 201, 202, 404, 405]
        )
        return status == 200 || status == 201 || status == 202
    }
}

// MARK: - Notification summary (what the OS actually permits)

public struct EVNotificationSummary: Codable, Sendable, Equatable {
    public let authorization: String
    public let pendingCount: Int
    public let note: String

    public init(authorization: String, pendingCount: Int, note: String) {
        self.authorization = authorization
        self.pendingCount = pendingCount
        self.note = note
    }
}

public enum EVNotificationInbox {
    /// The OS does not allow enumerating delivered notifications; the honest
    /// summary is authorization state plus the count of locally pending
    /// notification requests.
    public static func summary() async -> EVNotificationSummary {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        let authorization: String
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral: authorization = "granted"
        case .denied: authorization = "denied"
        case .notDetermined: authorization = "notDetermined"
        @unknown default: authorization = "unknown"
        }
        let pending = (try? await center.pendingNotificationRequests().count) ?? 0
        return EVNotificationSummary(
            authorization: authorization,
            pendingCount: pending,
            note: "Delivered-notification enumeration is not permitted by the OS; use backend alert endpoints for history."
        )
    }
}

// MARK: - Contacts

public struct EVContactMatch: Codable, Sendable, Equatable {
    public let id: String
    public let givenName: String
    public let familyName: String
    public let fullName: String
    public let phoneNumbers: [String]
    public let emailAddresses: [String]

    public init(
        id: String,
        givenName: String,
        familyName: String,
        fullName: String,
        phoneNumbers: [String],
        emailAddresses: [String]
    ) {
        self.id = id
        self.givenName = givenName
        self.familyName = familyName
        self.fullName = fullName
        self.phoneNumbers = phoneNumbers
        self.emailAddresses = emailAddresses
    }
}

public struct EVContactResolver: Sendable {
    public init() {}

    public func resolve(query: String, limit: Int = 10) async throws -> [EVContactMatch] {
        let store = CNContactStore()
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized:
            break
        case .notDetermined:
            guard try await store.requestAccess(for: .contacts) else {
                throw EVLifeError.permissionDenied("contacts")
            }
        default:
            throw EVLifeError.permissionDenied("contacts")
        }

        let keys: [CNKeyDescriptor] = [
            CNContactIdentifierKey as CNKeyDescriptor,
            CNContactGivenNameKey as CNKeyDescriptor,
            CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
        ]
        let request = CNContactFetchRequest(keysToFetch: keys)
        var matches: [EVContactMatch] = []
        let needle = query.lowercased()
        try store.enumerateContacts(with: request) { contact, _ in
            let fullName = "\(contact.givenName) \(contact.familyName)"
                .trimmingCharacters(in: .whitespaces)
            let phones = contact.phoneNumbers.map { $0.value.stringValue }
            let emails = contact.emailAddresses.map { $0.value as String }
            let haystack = "\(fullName) \(phones.joined(separator: " ")) \(emails.joined(separator: " "))"
                .lowercased()
            guard haystack.contains(needle) else { return }
            matches.append(EVContactMatch(
                id: contact.identifier,
                givenName: contact.givenName,
                familyName: contact.familyName,
                fullName: fullName,
                phoneNumbers: phones,
                emailAddresses: emails
            ))
        }
        return Array(matches.prefix(limit))
    }
}

// MARK: - Messages / calls (URL + framework paths)

public enum EVMessageURLs {
    public static func smsURL(recipients: [String], body: String?) -> URL? {
        let joined = recipients.joined(separator: ",")
        var components = URLComponents(string: "sms:\(joined)")
        if let body {
            components?.queryItems = [URLQueryItem(name: "body", value: body)]
        }
        return components?.url
    }
}

#if canImport(MessageUI)
public enum EVMessageComposer {
    /// True when this device can present the system SMS compose sheet.
    public static func canSendText() -> Bool {
        MFMessageComposeViewController.canSendText()
    }

    public static func canSendMail() -> Bool {
        MFMailComposeViewController.canSendMail()
    }
}
#endif

public enum EVCallPlacer {
    public static func placeCall(destination: String, kind: String = "tel") async throws {
        let scheme = kind == "facetime" ? "facetime" : "tel"
        guard let url = URL(string: "\(scheme)://\(destination)") else {
            throw EVLifeError.invalidArgument("could not build \(scheme) URL")
        }
        #if canImport(UIKit)
        guard UIApplication.shared.canOpenURL(url) else {
            throw EVLifeError.unavailable("\(scheme) is not available on this device")
        }
        await UIApplication.shared.open(url)
        #elseif os(macOS)
        guard NSWorkspace.shared.open(url) else {
            throw EVLifeError.unavailable("\(scheme) is not available on this Mac")
        }
        #else
        throw EVLifeError.unavailable("call placement is not available on this platform")
        #endif
    }
}
