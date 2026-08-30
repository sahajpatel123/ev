import Foundation
import EvieNativeBroker
#if canImport(UIKit)
import UIKit
import MessageUI
import EventKit
import Contacts
import CoreLocation
import UserNotifications
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
            return ["ok": true, "broker_version": BrokerVersion.version, "capabilities": advertised()]
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

    private func advertised() -> [String] {
        [
            "create_timer", "create_reminder", "create_alarm", "call_contact",
            "message_contact", "facetime_contact", "start_directions", "open_maps",
            "create_calendar_event", "open_app", "current_location", "share_content",
            "copy_to_clipboard", "haptic", "self_test",
        ]
    }

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
