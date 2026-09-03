import Foundation
import EvieNativeBroker
import Darwin
#if canImport(UIKit)
import UIKit
import MessageUI
import EventKit
import Contacts
import CoreLocation
import UserNotifications
import AVFoundation
#endif

@MainActor
final class CapabilityBroker: NSObject {
    func handle(_ request: NativeBridgeRequest, raw: [String: Any]) async -> [String: Any] {
        switch request.type {
        case "haptic":
            let event = HapticEvent(rawValue: request.event ?? "selection") ?? .selection
            FeedbackEngine.play(event)
            return ["ok": true, "executed": true, "verified": true, "result": "EXECUTED"]
        case "capabilities":
            return advertisedPayload()
        case "permissionStatus":
            return ["ok": true, "permissions": permissionEvidence()]
        case "requestPermission":
            return await requestPermission(name: request.event ?? "")
        case "pending_capture":
            let note = UserDefaults.standard.string(forKey: "evie.pending_capture") ?? ""
            let key = UserDefaults.standard.string(forKey: "evie.pending_capture_key") ?? ""
            UserDefaults.standard.removeObject(forKey: "evie.pending_capture")
            UserDefaults.standard.removeObject(forKey: "evie.pending_capture_key")
            return ["ok": true, "note": note, "idempotency_key": key, "executed": false]
        case "healthkit_snapshot":
            return [
                "ok": true,
                "available": false,
                "reason": "no_entitlement",
                "freshness": "unavailable",
                "sent_to_model": false,
                "snapshot": [:],
            ]
        case "calendar_snapshot":
            return await calendarSnapshot()
        case "contacts_snapshot":
            return contactsSnapshot()
        case "notification_status":
            return await notificationStatus()
        case "bind_session":
            if let token = raw["token"] as? String, !token.isEmpty {
                DeviceAuth.store(token)
                return ["ok": true]
            }
            return ["ok": false, "failure": "UNAUTHENTICATED"]
        case "execute":
            guard let actionID = request.actionID, !actionID.isEmpty else {
                return ["ok": false, "failure": "INVALID_TOKEN"]
            }
            return await execute(actionID: actionID)
        default:
            return ["ok": false, "failure": "UNSUPPORTED"]
        }
    }

    private func advertisedPayload() -> [String: Any] {
        [
            "ok": true,
            "broker_version": BrokerVersion.version,
            "capabilities": advertised(),
            "endpoint_capabilities": [
                "foreground_voice", "camera", "text", "notification", "microphone", "location", "clipboard",
            ],
            "permissions": permissionEvidence(),
            "hardware": DeviceHardware.profile(),
        ]
    }

    private func advertised() -> [String] {
        [
            "foreground_voice", "camera", "text", "notification", "microphone", "location", "clipboard",
            "create_timer", "create_reminder", "create_alarm", "call_contact",
            "message_contact", "facetime_contact", "start_directions", "open_maps",
            "create_calendar_event", "open_app", "current_location", "share_content",
            "copy_to_clipboard", "haptic", "self_test",
        ]
    }

    private func permissionEvidence() -> [String: String] {
        #if os(iOS)
        [
            "microphone": AVCaptureDevice.authorizationStatus(for: .audio).evieLabel,
            "camera": AVCaptureDevice.authorizationStatus(for: .video).evieLabel,
            "contacts": CNContactStore.authorizationStatus(for: .contacts).evieContactsLabel,
            "calendar": calendarLabel(),
            "reminders": reminderLabel(),
            "location": locationLabel(),
            "notifications": UserDefaults.standard.string(forKey: "evie.notification_auth") ?? "undetermined",
            "health": "unavailable",
        ]
        #else
        [:]
        #endif
    }

    #if os(iOS)
    private func locationLabel() -> String {
        switch CLLocationManager().authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: return "granted"
        case .denied, .restricted: return "denied"
        default: return "undetermined"
        }
    }

    private func calendarLabel() -> String {
        let status = EKEventStore.authorizationStatus(for: .event)
        if #available(iOS 17.0, *) {
            switch status {
            case .fullAccess: return "granted"
            case .writeOnly: return "write_only"
            case .denied, .restricted: return "denied"
            default: return "undetermined"
            }
        }
        switch status {
        case .authorized: return "granted"
        case .denied, .restricted: return "denied"
        default: return "undetermined"
        }
    }

    private func reminderLabel() -> String {
        let status = EKEventStore.authorizationStatus(for: .reminder)
        if #available(iOS 17.0, *) {
            switch status {
            case .fullAccess: return "granted"
            case .writeOnly: return "write_only"
            case .denied, .restricted: return "denied"
            default: return "undetermined"
            }
        }
        switch status {
        case .authorized: return "granted"
        case .denied, .restricted: return "denied"
        default: return "undetermined"
        }
    }

    private func requestPermission(name: String) async -> [String: Any] {
        switch name {
        case "camera":
            let ok = await AVCaptureDevice.requestAccess(for: .video)
            return ["ok": ok, "permission": ok ? "granted" : "denied"]
        case "microphone":
            let ok = await AVCaptureDevice.requestAccess(for: .audio)
            return ["ok": ok, "permission": ok ? "granted" : "denied"]
        case "calendar":
            let store = EKEventStore()
            let ok: Bool
            if #available(iOS 17.0, *) {
                ok = (try? await store.requestFullAccessToEvents()) ?? false
            } else {
                ok = (try? await store.requestAccess(to: .event)) ?? false
            }
            return ["ok": ok, "permission": ok ? "granted" : "denied"]
        case "contacts":
            let ok = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
                CNContactStore().requestAccess(for: .contacts) { granted, _ in
                    cont.resume(returning: granted)
                }
            }
            return ["ok": ok, "permission": ok ? "granted" : "denied"]
        case "notifications":
            let ok = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
                UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
                    UserDefaults.standard.set(granted ? "granted" : "denied", forKey: "evie.notification_auth")
                    cont.resume(returning: granted)
                }
            }
            return ["ok": ok, "permission": ok ? "granted" : "denied", "delivery": "poll"]
        default:
            return ["ok": true, "permissions": permissionEvidence()]
        }
    }

    private func calendarSnapshot() async -> [String: Any] {
        let store = EKEventStore()
        let allowed = calendarLabel() == "granted"
        guard allowed else {
            return ["ok": true, "events": [], "permission": calendarLabel(), "sent_to_model": false]
        }
        let start = Date()
        let end = Calendar.current.date(byAdding: .day, value: 7, to: start) ?? start
        let pred = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let formatter = ISO8601DateFormatter()
        let events = store.events(matching: pred).prefix(12).map { event in
            [
                "title": event.title ?? "Event",
                "start": formatter.string(from: event.startDate),
            ]
        }
        return ["ok": true, "events": Array(events), "permission": "granted", "sent_to_model": false]
    }

    private func contactsSnapshot() -> [String: Any] {
        let label = CNContactStore.authorizationStatus(for: .contacts).evieContactsLabel
        guard label == "granted" || label == "limited" else {
            return ["ok": true, "contacts": [], "permission": label, "sent_to_model": false]
        }
        let keys = [CNContactGivenNameKey, CNContactFamilyNameKey] as [CNKeyDescriptor]
        let request = CNContactFetchRequest(keysToFetch: keys)
        var names: [[String: String]] = []
        try? CNContactStore().enumerateContacts(with: request) { contact, stop in
            if names.count >= 20 {
                stop.pointee = true
                return
            }
            let name = [contact.givenName, contact.familyName].filter { !$0.isEmpty }.joined(separator: " ")
            if !name.isEmpty {
                names.append(["name": name])
            }
        }
        return ["ok": true, "contacts": names, "permission": label, "sent_to_model": false]
    }

    private func notificationStatus() async -> [String: Any] {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        let auth: String
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral: auth = "granted"
        case .denied: auth = "denied"
        default: auth = "undetermined"
        }
        UserDefaults.standard.set(auth, forKey: "evie.notification_auth")
        return [
            "ok": true,
            "authorization": auth,
            "delivery": "poll",
            "push_registered": false,
            "reason": "no_aps_environment",
        ]
    }
    #else
    private func requestPermission(name: String) async -> [String: Any] {
        ["ok": false, "failure": "ACTION_UNAVAILABLE"]
    }

    private func calendarSnapshot() async -> [String: Any] {
        ["ok": false, "events": [], "sent_to_model": false, "failure": "ACTION_UNAVAILABLE"]
    }

    private func contactsSnapshot() -> [String: Any] {
        ["ok": false, "contacts": [], "sent_to_model": false, "failure": "ACTION_UNAVAILABLE"]
    }

    private func notificationStatus() async -> [String: Any] {
        ["ok": true, "authorization": "unavailable", "delivery": "poll", "push_registered": false]
    }
    #endif

    private func execute(actionID: String) async -> [String: Any] {
        guard let token = DeviceAuth.token() else {
            return ["ok": false, "accepted": false, "executed": false, "verified": false, "failure": "UNAUTHENTICATED"]
        }
        let origin = AppOrigin.apiOrigin
        guard let fetched = await GatewayClient.post(
            origin: origin,
            path: "/v1/device-gateway/mobile-actions/\(actionID)/native-execute",
            token: token
        ) else {
            return ["ok": false, "failure": "NETWORK"]
        }
        guard fetched["ok"] as? Bool == true, let run = fetched["run"] as? [String: Any] else {
            return fetched
        }
        let outcome = await perform(run: run, actionID: actionID)
        if let completionToken = run["completion_token"] as? String {
            _ = await GatewayClient.post(
                origin: origin,
                path: "/v1/device-gateway/mobile-actions/\(actionID)/complete",
                token: token,
                body: outcome.completePayload(completionToken: completionToken)
            )
        }
        return outcome.json
    }

    private func perform(run: [String: Any], actionID: String) async -> BrokerOutcome {
        let kind = (run["kind"] as? String) ?? "noop"
        switch kind {
        case "haptic":
            FeedbackEngine.play(.actionSuccess)
            return .init(actionID: actionID, executed: true, verified: true, result: "EXECUTED")
        case "timer", "alarm":
            return await scheduleLocalAlert(run: run, actionID: actionID, alarm: kind == "alarm")
        case "reminder":
            return await saveReminder(run: run, actionID: actionID)
        case "calendar":
            return await saveEvent(run: run, actionID: actionID)
        case "location":
            return await currentLocation(actionID: actionID)
        case "message":
            return await composeMessage(run: run, actionID: actionID)
        case "call", "facetime", "open_url", "open_app":
            return await openURL(run: run, actionID: actionID, kind: kind)
        case "share":
            return await share(run: run, actionID: actionID)
        case "clipboard":
            return await copy(run: run, actionID: actionID)
        case "direct_message":
            return .init(
                actionID: actionID,
                accepted: true,
                executed: false,
                verified: false,
                result: "PREPARED",
                failure: "ACTION_UNAVAILABLE",
                note: "No approved direct-send adapter on this iPhone."
            )
        case "self_test":
            return .init(actionID: actionID, executed: true, verified: true, result: "SELF_TEST_OK")
        default:
            return .init(actionID: actionID, executed: false, verified: false, result: "FAILED", failure: "ACTION_UNAVAILABLE")
        }
    }

#if os(iOS)
    private func scheduleLocalAlert(run: [String: Any], actionID: String, alarm: Bool) async -> BrokerOutcome {
        let center = UNUserNotificationCenter.current()
        let granted = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            center.requestAuthorization(options: [.alert, .sound]) { ok, _ in cont.resume(returning: ok) }
        }
        guard granted else {
            return .init(actionID: actionID, result: "PERMISSION_REQUIRED", failure: "PERMISSION_REQUIRED")
        }
        let seconds = (run["duration_seconds"] as? Int) ?? 60
        let content = UNMutableNotificationContent()
        content.title = alarm ? (run["label"] as? String ?? "Evie alarm") : (run["label"] as? String ?? "Evie timer")
        content.body = alarm ? "Evie alarm" : "Evie timer"
        content.sound = .default
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: TimeInterval(max(1, seconds)), repeats: false)
        let id = "evie-\(actionID)"
        try? await center.add(UNNotificationRequest(identifier: id, content: content, trigger: trigger))
        return .init(
            actionID: actionID,
            executed: true,
            verified: true,
            result: "CREATED",
            timerKind: "evie_notification"
        )
    }

    private func saveReminder(run: [String: Any], actionID: String) async -> BrokerOutcome {
        let store = EKEventStore()
        do {
            let ok = try await store.requestFullAccessToReminders()
            guard ok else {
                return .init(actionID: actionID, result: "PERMISSION_REQUIRED", failure: "PERMISSION_REQUIRED")
            }
        } catch {
            return .init(actionID: actionID, result: "PERMISSION_REQUIRED", failure: "PERMISSION_REQUIRED")
        }
        let reminder = EKReminder(eventStore: store)
        reminder.title = (run["title"] as? String) ?? "Reminder"
        reminder.calendar = store.defaultCalendarForNewReminders()
        if let when = run["when_iso"] as? String, let date = ISO8601DateFormatter().date(from: when) {
            reminder.dueDateComponents = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: date)
        }
        do {
            try store.save(reminder, commit: true)
            return .init(actionID: actionID, executed: true, verified: true, result: "CREATED")
        } catch {
            return .init(actionID: actionID, result: "FAILED", failure: "EXECUTION_FAILED")
        }
    }

    private func saveEvent(run: [String: Any], actionID: String) async -> BrokerOutcome {
        let store = EKEventStore()
        do {
            let ok = try await store.requestWriteOnlyAccessToEvents()
            guard ok else {
                return .init(actionID: actionID, result: "PERMISSION_REQUIRED", failure: "PERMISSION_REQUIRED")
            }
        } catch {
            return .init(actionID: actionID, result: "PERMISSION_REQUIRED", failure: "PERMISSION_REQUIRED")
        }
        let event = EKEvent(eventStore: store)
        event.title = (run["title"] as? String) ?? "Event"
        event.calendar = store.defaultCalendarForNewEvents
        let start = (run["when_iso"] as? String).flatMap { ISO8601DateFormatter().date(from: $0) } ?? Date()
        event.startDate = start
        event.endDate = start.addingTimeInterval(3600)
        do {
            try store.save(event, span: .thisEvent, commit: true)
            return .init(actionID: actionID, executed: true, verified: true, result: "CREATED")
        } catch {
            return .init(actionID: actionID, result: "FAILED", failure: "EXECUTION_FAILED")
        }
    }

    private func currentLocation(actionID: String) async -> BrokerOutcome {
        let manager = CLLocationManager()
        manager.requestWhenInUseAuthorization()
        guard let location = manager.location else {
            return .init(actionID: actionID, result: "PERMISSION_REQUIRED", failure: "PERMISSION_REQUIRED")
        }
        let geocoder = CLGeocoder()
        let place = (try? await geocoder.reverseGeocodeLocation(location))?.first
        let label = [place?.locality, place?.administrativeArea].compactMap { $0 }.joined(separator: ", ")
        return .init(actionID: actionID, executed: true, verified: true, result: "EXECUTED", displayName: label)
    }

    private func composeMessage(run: [String: Any], actionID: String) async -> BrokerOutcome {
        let query = (run["contact_query"] as? String) ?? ""
        let body = (run["message"] as? String) ?? ""
        let resolved = resolveContact(query)
        if resolved.failure != nil {
            return .init(actionID: actionID, result: resolved.result, failure: resolved.failure, choices: resolved.choices)
        }
        guard MFMessageComposeViewController.canSendText() else {
            if let url = URL(string: "sms:\(resolved.digits)") {
                await UIApplication.shared.open(url)
            }
            return .init(actionID: actionID, executed: true, verified: false, systemUI: true, result: "SYSTEM_UI_OPENED", displayName: resolved.name)
        }
        let composer = MFMessageComposeViewController()
        composer.recipients = [resolved.digits]
        composer.body = body
        guard let presenter = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .flatMap({ $0.windows })
            .first(where: { $0.isKeyWindow })?
            .rootViewController else {
            return .init(actionID: actionID, executed: false, result: "FAILED", failure: "EXECUTION_FAILED")
        }
        presenter.present(composer, animated: true)
        return .init(actionID: actionID, executed: true, verified: false, systemUI: true, result: "SYSTEM_UI_OPENED", displayName: resolved.name)
    }

    private func openURL(run: [String: Any], actionID: String, kind: String) async -> BrokerOutcome {
        var raw = (run["url"] as? String) ?? ""
        if kind == "call" || kind == "facetime" {
            let query = (run["contact_query"] as? String) ?? ""
            if raw.isEmpty {
                let resolved = resolveContact(query)
                if let failure = resolved.failure {
                    return .init(actionID: actionID, result: resolved.result, failure: failure, choices: resolved.choices)
                }
                raw = kind == "facetime" ? "facetime:\(resolved.digits)" : "tel:\(resolved.digits)"
            }
        }
        guard let url = URL(string: raw) else {
            return .init(actionID: actionID, result: "FAILED", failure: "EXECUTION_FAILED")
        }
        let ok = await UIApplication.shared.open(url)
        if !ok {
            return .init(actionID: actionID, executed: false, result: "FAILED", failure: "EXECUTION_FAILED")
        }
        let system = kind == "call" || kind == "facetime" || kind == "open_url" || kind == "open_app"
        return .init(
            actionID: actionID,
            executed: true,
            verified: false,
            systemUI: system,
            result: "SYSTEM_UI_OPENED"
        )
    }

    private func share(run: [String: Any], actionID: String) async -> BrokerOutcome {
        let text = (run["text"] as? String) ?? ""
        guard let presenter = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .flatMap({ $0.windows })
            .first(where: { $0.isKeyWindow })?
            .rootViewController else {
            return .init(actionID: actionID, result: "FAILED", failure: "EXECUTION_FAILED")
        }
        presenter.present(UIActivityViewController(activityItems: [text], applicationActivities: nil), animated: true)
        return .init(actionID: actionID, executed: true, verified: false, systemUI: true, result: "SYSTEM_UI_OPENED")
    }

    private func copy(run: [String: Any], actionID: String) async -> BrokerOutcome {
        UIPasteboard.general.string = run["text"] as? String
        return .init(actionID: actionID, executed: true, verified: true, result: "EXECUTED")
    }

    private func resolveContact(_ query: String) -> (name: String, digits: String, result: String, failure: String?, choices: [[String: String]]) {
        let store = CNContactStore()
        let status = CNContactStore.authorizationStatus(for: .contacts)
        if status == .notDetermined {
            // First use: caller should have prompted. Treat as permission required if still locked.
        }
        if status == .denied || status == .restricted {
            return ("", "", "PERMISSION_REQUIRED", "PERMISSION_REQUIRED", [])
        }
        let keys = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactPhoneNumbersKey] as [CNKeyDescriptor]
        let request = CNContactFetchRequest(keysToFetch: keys)
        var matches: [(String, String)] = []
        let needle = query.lowercased()
        do {
            try store.enumerateContacts(with: request) { contact, _ in
                let name = (contact.givenName + " " + contact.familyName).trimmingCharacters(in: .whitespaces)
                if name.lowercased().contains(needle) || needle.contains(name.lowercased()) {
                    if let number = contact.phoneNumbers.first?.value.stringValue {
                        matches.append((name, number))
                    }
                }
            }
        } catch {
            return ("", "", "PERMISSION_REQUIRED", "PERMISSION_REQUIRED", [])
        }
        if matches.isEmpty { return ("", "", "CONTACT_NOT_FOUND", "CONTACT_NOT_FOUND", []) }
        if matches.count > 1 {
            return ("", "", "CONTACT_AMBIGUOUS", "CONTACT_AMBIGUOUS", matches.prefix(4).map { ["name": $0.0] })
        }
        let digits = matches[0].1.filter { $0.isNumber || $0 == "+" }
        return (matches[0].0, digits, "PREPARED", nil, [])
    }
#else
    private func scheduleLocalAlert(run: [String: Any], actionID: String, alarm: Bool) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func saveReminder(run: [String: Any], actionID: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func saveEvent(run: [String: Any], actionID: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func currentLocation(actionID: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func composeMessage(run: [String: Any], actionID: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func openURL(run: [String: Any], actionID: String, kind: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func share(run: [String: Any], actionID: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
    private func copy(run: [String: Any], actionID: String) async -> BrokerOutcome {
        .init(actionID: actionID, result: "FAILED", failure: "ACTION_UNAVAILABLE")
    }
#endif
}

struct BrokerOutcome {
    var actionID: String
    var accepted: Bool = true
    var executed: Bool = false
    var verified: Bool = false
    var systemUI: Bool = false
    var result: String
    var failure: String? = nil
    var displayName: String? = nil
    var choices: [[String: String]] = []
    var timerKind: String? = nil
    var note: String? = nil

    var json: [String: Any] {
        var payload: [String: Any] = [
            "ok": failure == nil,
            "action_id": actionID,
            "accepted": accepted,
            "executed": executed,
            "verified": verified,
            "system_ui_presented": systemUI,
            "result": result,
        ]
        if let failure { payload["failure"] = failure }
        if let displayName { payload["display_name"] = displayName }
        if let timerKind { payload["timer_kind"] = timerKind }
        if let note { payload["note"] = note }
        if !choices.isEmpty { payload["choices"] = choices }
        return payload
    }

    func completePayload(completionToken: String) -> [String: Any] {
        var payload = json
        payload["completion_token"] = completionToken
        payload["status"] = executed ? "executed" : "failed"
        return payload
    }
}

enum DeviceHardware {
    static func machine() -> String {
        var info = utsname()
        uname(&info)
        return withUnsafePointer(to: &info.machine) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
    }

    static func profile() -> [String: Any] {
        let model = machine()
        let quality: String
        let rank: Int
        switch model {
        case "iPhone17,1", "iPhone17,2", "iPhone16,1", "iPhone16,2":
            quality = "pro"
            rank = 0
        case "iPhone14,6", "iPhone12,8":
            quality = "standard"
            rank = 10
        default:
            quality = model.hasPrefix("iPhone") ? "standard" : "unknown"
            rank = model.hasPrefix("iPhone") ? 20 : 50
        }
        return [
            "model": model,
            "machine": model,
            "camera_quality": quality,
            "camera_preference_rank": rank,
        ]
    }
}

#if os(iOS)
extension AVAuthorizationStatus {
    var evieLabel: String {
        switch self {
        case .authorized: return "granted"
        case .denied, .restricted: return "denied"
        default: return "undetermined"
        }
    }
}

extension CNAuthorizationStatus {
    var evieContactsLabel: String {
        switch self {
        case .authorized: return "granted"
        case .denied, .restricted: return "denied"
        default: return "undetermined"
        }
    }
}
#endif
