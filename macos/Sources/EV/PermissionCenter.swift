import ApplicationServices
import AppKit
import AVFoundation
import Contacts
import EVRuntime
import CoreBluetooth
import CoreGraphics
import CoreLocation
import CoreServices
import EventKit
import Combine
import Foundation
import IOKit.hid
import ScreenCaptureKit
import Security
import Speech
import SwiftUI
import UserNotifications

/// Every TCC permission the LIFE access hub can need. Detection is live —
/// the panel never claims a permission is granted when TCC reports denied.
enum PermissionKind: String, CaseIterable, Identifiable {
    case microphone
    case speechRecognition
    case camera
    case screenRecording
    case accessibility
    case automation
    case fullDiskAccess
    case contacts
    case calendars
    case reminders
    case notifications
    case bluetooth
    case inputMonitoring
    case location

    var id: String { rawValue }
}

enum PermissionState: String {
    case granted
    case denied
    case notDetermined
    case restricted
    case partial
}

struct PermissionStatus {
    let kind: PermissionKind
    let state: PermissionState
    let whatBreaks: String
    let settingsURL: URL?
    let canRequest: Bool
    /// When set, macOS reports the permission as missing even though a grant
    /// exists — typically because the recorded grant is tied to an older
    /// build's code signature. Explains how to repair it in System Settings.
    var repairHint: String?
}

/// Instantiating a `CBCentralManager` is what triggers macOS's Bluetooth
/// authorization prompt. The instance must be retained for the prompt to
/// resolve; `CBManager.authorization` reports the live result afterwards.
final class BluetoothAuthorizationRequester: NSObject, CBCentralManagerDelegate {
    static let shared = BluetoothAuthorizationRequester()
    private var manager: CBCentralManager?

    func request() {
        guard manager == nil else { return }
        manager = CBCentralManager(delegate: self, queue: .main)
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        // State is surfaced through CBManager.authorization; nothing to do.
    }
}

/// Detects every permission the LIFE hub needs, explains what breaks when
/// denied, deep-links to the exact System Settings pane, and offers
/// programmatic TCC requests where the OS provides them.
enum PermissionCenter {
    static func statuses() async -> [PermissionStatus] {
        [
            microphoneStatus(),
            speechStatus(),
            cameraStatus(),
            screenRecordingStatus(),
            accessibilityStatus(),
            automationStatus(),
            fullDiskAccessStatus(),
            contactsStatus(),
            calendarsStatus(),
            remindersStatus(),
            await notificationStatus(),
            bluetoothStatus(),
            inputMonitoringStatus(),
            locationStatus(),
        ]
    }

    static func openSettings(for kind: PermissionKind) {
        guard let url = settingsURL(for: kind) else { return }
        NSWorkspace.shared.open(url)
    }

    static func settingsURL(for kind: PermissionKind) -> URL? {
        let string: String
        switch kind {
        case .microphone:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        case .speechRecognition:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition"
        case .camera:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera"
        case .screenRecording:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        case .accessibility:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        case .automation:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
        case .fullDiskAccess:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
        case .contacts:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts"
        case .calendars:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars"
        case .reminders:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders"
        case .notifications:
            // macOS 13+ System Settings exposes the Notifications pane as an
            // ExtensionKit pane; the legacy `com.apple.preference.Notifications`
            // identifier no longer opens anything.
            string = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
        case .bluetooth:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth"
        case .inputMonitoring:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        case .location:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices"
        }
        return URL(string: string)
    }

    /// Programmatic TCC request where the OS offers one. Returns true when a
    /// request was made or already granted; manual-only kinds open Settings.
    static func request(_ kind: PermissionKind) async -> Bool {
        switch kind {
        case .microphone:
            return await MicrophoneAuthorization.requestAccess()
        case .camera:
            return await AVCaptureDevice.requestAccess(for: .video)
        case .notifications:
            return (try? await UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .sound, .badge]
            )) ?? false
        case .speechRecognition:
            return await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { _ in
                    continuation.resume(returning: true)
                }
            }
        case .contacts:
            return (try? await CNContactStore().requestAccess(for: .contacts)) ?? false
        case .calendars:
            return (try? await EKEventStore().requestFullAccessToEvents()) ?? false
        case .reminders:
            return (try? await EKEventStore().requestFullAccessToReminders()) ?? false
        case .screenRecording:
            // CGRequestScreenCaptureAccess registers EV in the Screen Recording
            // pane. On modern macOS the legacy alert may not be shown, but the
            // registration still happens — so call it unconditionally.
            _ = CGRequestScreenCaptureAccess()
            // Also poke ScreenCaptureKit: on macOS 13+ a shareable-content
            // query is what actually makes the app appear in the pane when the
            // CG prompt path is deprecated or suppressed for accessory apps.
            _ = Task {
                _ = try? await SCShareableContent.excludingDesktopWindows(
                    false,
                    onScreenWindowsOnly: false
                )
            }
            try? await Task.sleep(nanoseconds: 600_000_000)
            return CGPreflightScreenCaptureAccess()
        case .inputMonitoring:
            // IOHIDRequestAccess presents the Input Monitoring prompt and
            // registers EV in the pane. Creating an event tap is a second
            // registration path in case the IOHID prompt is suppressed for an
            // accessory (menu-bar) app; the tap is dropped right after.
            let hidResult = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
            let tap = CGEvent.tapCreate(
                tap: .cgSessionEventTap,
                place: .headInsertEventTap,
                options: CGEventTapOptions.listenOnly,
                eventsOfInterest: CGEventMask(1 << CGEventType.keyDown.rawValue),
                callback: { _, _, _, _ in nil },
                userInfo: nil
            )
            if let tap {
                CGEvent.tapEnable(tap: tap, enable: true)
                try? await Task.sleep(nanoseconds: 200_000_000)
                CGEvent.tapEnable(tap: tap, enable: false)
            }
            if hidResult { return true }
            return IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted
        case .automation:
            return await requestAutomationAll()
        case .location:
            locationManager.requestWhenInUseAuthorization()
            return true
        case .bluetooth:
            await MainActor.run { BluetoothAuthorizationRequester.shared.request() }
            // Give the CoreBluetooth prompt a beat to appear; live state is
            // read via CBManager.authorization on the next status refresh.
            try? await Task.sleep(nanoseconds: 500_000_000)
            return true
        case .accessibility:
            // Present the standard "control this computer using accessibility
            // features" consent dialog, then re-check for a few seconds so a
            // fresh grant (or a repaired toggle in System Settings) is
            // reflected without requiring a manual refresh.
            await activateForPrompt()
            let options = [
                kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true,
            ] as CFDictionary
            if AXIsProcessTrustedWithOptions(options) {
                return true
            }
            openSettings(for: kind)
            for _ in 0..<20 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if AXIsProcessTrusted() { return true }
            }
            return AXIsProcessTrusted()
        case .fullDiskAccess:
            openSettings(for: kind)
            return false
        }
    }

    /// Version stamped into `ev.permissions.autoRequestedVersion` whenever the
    /// registration sweep runs. Bump this when the request set or order
    /// changes so every installed build re-runs the sweep exactly once and EV
    /// re-registers in any new System Settings pane.
    static let registrationVersion = 2

    /// Requests every permission macOS exposes a programmatic prompt for, in
    /// a safe order, so EV registers in each System Settings pane. After this
    /// returns, the panes list EV and the user toggles any that are still off.
    /// Accessibility and Full Disk Access have no prompt — their panes are the
    /// ones with a "+" button — so they are intentionally left to the user.
    ///
    /// TCC registers an app in a pane only after the app has actually asked
    /// for that permission, so every programmatic request is fired here (and
    /// automation asks each target app separately so Messages, Mail, and
    /// System Events each appear in the Automation list).
    ///
    /// The app is activated first: consent dialogs from a menu-bar accessory
    /// present reliably only when the process is active. Most requests then
    /// block on the user's answer (AVFoundation, Contacts, EventKit, speech),
    /// which paces the prompts naturally; the explicit gaps cover the ones
    /// that return immediately (screen recording, input monitoring, Bluetooth).
    static func requestAll() async -> [PermissionStatus] {
        await MainActor.run { NSApp.activate() }
        let order: [PermissionKind] = [
            .microphone,
            .speechRecognition,
            .camera,
            .screenRecording,
            .contacts,
            .calendars,
            .reminders,
            .notifications,
            .inputMonitoring,
            .bluetooth,
            .location,
        ]
        for kind in order {
            _ = await request(kind)
            // A short gap lets macOS settle each consent prompt before the
            // next one fires; prompts that block on user input pace themselves.
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        // Automation is per-target: request(.automation) prompts System Events
        // directly and launches Messages/Mail so every target gets its own
        // consent prompt (and row) in the Automation pane.
        _ = await request(.automation)
        _ = await request(.accessibility)
        // Let any final consent prompts settle before reporting statuses.
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        return await statuses()
    }

    /// Re-runs registration for everything macOS still reports as undecided.
    /// Idempotent — decided permissions are never re-prompted — so re-opening
    /// the panel (or re-running the CLI probe) fills in any pane EV has not
    /// appeared in yet instead of re-asking about ones already answered.
    static func requestPending() async -> [PermissionStatus] {
        var current = await statuses()
        // Automation is per-target: even when the aggregate row is partial
        // (some targets granted, others still undecided) it must be re-requested
        // so a just-launched Messages/Mail gets its own consent prompt. It is
        // not part of `needsRequest`, so track it separately to avoid the early
        // return below skipping it.
        let needsAutomationRefresh = current.contains {
            $0.kind == .automation && $0.state == .partial
        }
        let toRequest = current.filter { needsRequest($0) }
        guard !toRequest.isEmpty || needsAutomationRefresh else { return current }
        await MainActor.run { NSApp.activate() }
        for status in toRequest {
            _ = await request(status.kind)
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        if needsAutomationRefresh {
            _ = await request(.automation)
        }
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        current = await statuses()
        return current
    }

    private static func needsRequest(_ status: PermissionStatus) -> Bool {
        switch status.state {
        case .notDetermined:
            return true
        case .denied:
            // Screen Recording has no "undecided" state — CGPreflight returns
            // denied until granted — so a request is the only way to register
            // EV in that pane. Accessibility is the same: EV must ask so it
            // appears in the + list, then the owner toggles it.
            return status.kind == .screenRecording || status.kind == .accessibility
        case .partial, .granted, .restricted:
            return false
        }
    }

    // MARK: - Individual checks

    private static func permissionState(_ grant: MicrophoneGrant) -> PermissionState {
        switch grant {
        case .granted: return .granted
        case .denied: return .denied
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        }
    }

    private static func microphoneStatus() -> PermissionStatus {
        MicrophoneAuthorization.publishCurrent()
        let state = permissionState(MicrophoneAuthorization.current())
        return PermissionStatus(
            kind: .microphone,
            state: state,
            whatBreaks: "EV cannot hear you. Grant Microphone so the open app can listen continuously.",
            settingsURL: settingsURL(for: .microphone),
            canRequest: true
        )
    }

    private static func speechStatus() -> PermissionStatus {
        let state: PermissionState
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .speechRecognition,
            state: state,
            whatBreaks: "On-device dictation / speech-to-text for quick capture cannot run.",
            settingsURL: settingsURL(for: .speechRecognition),
            canRequest: true
        )
    }

    private static func cameraStatus() -> PermissionStatus {
        let state: PermissionState
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .camera,
            state: state,
            whatBreaks: "Camera capture (photo/video notes) cannot run. Text and file capture still work.",
            settingsURL: settingsURL(for: .camera),
            canRequest: true
        )
    }

    private static func screenRecordingStatus() -> PermissionStatus {
        let state: PermissionState = CGPreflightScreenCaptureAccess() ? .granted : .denied
        return PermissionStatus(
            kind: .screenRecording,
            state: state,
            whatBreaks: "ScreenCaptureKit-based ambient context (Agent 13's macOS collectors) cannot see the screen.",
            settingsURL: settingsURL(for: .screenRecording),
            canRequest: true
        )
    }

    private static func accessibilityStatus() -> PermissionStatus {
        let trusted = AXIsProcessTrusted()
        let stale = !trusted && accessibilityGrantIsStale()
        return PermissionStatus(
            kind: .accessibility,
            state: trusted ? .granted : .denied,
            whatBreaks: "Evie can open and close apps, but cannot click, type, or inspect controls inside them. The ⇧⌘E hotkey also cannot see keys from other apps.",
            settingsURL: settingsURL(for: .accessibility),
            canRequest: true,
            repairHint: stale ? accessibilityRepairHint : nil
        )
    }

    // MARK: - TCC inspection (stale-grant detection)

    /// TCC ties each grant to the code signature of the app that asked. If EV
    /// was granted Accessibility while a differently-signed build was running
    /// (or the grant was recorded against an ad-hoc binary's cdhash), TCC
    /// stores a record the current build can never match — `AXIsProcessTrusted()`
    /// returns false forever no matter how many times the toggle is flipped.
    /// We detect that by comparing the recorded csreq against our own current
    /// designated requirement and tell the user to remove + re-add EV.
    private static let accessibilityRepairHint =
        "macOS recorded your Accessibility grant against an older build of EV (code-signature mismatch). "
        + "Open the Accessibility pane, remove EV, toggle it back on, then refresh — the rebuilt app re-registers "
        + "under its current signature."

    private static func accessibilityGrantIsStale() -> Bool {
        guard let record = tccAccessRecord(authValue: 2) else { return false }
        guard let recordedBlob = record.csreq,
              let currentBlob = currentDesignatedRequirementData() else {
            // Without a comparable csreq we cannot claim staleness.
            return false
        }
        return recordedBlob != currentBlob
    }

    /// The current process's designated requirement, in the same on-disk form
    /// TCC stores in `access.csreq`. Byte comparison is exact: an identical
    /// blob means the grant matches the running build.
    private static func currentDesignatedRequirementData() -> Data? {
        var code: SecCode?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code else { return nil }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode else { return nil }
        var requirement: SecRequirement?
        guard SecCodeCopyDesignatedRequirement(staticCode, [], &requirement) == errSecSuccess, let requirement else { return nil }
        var data: CFData?
        guard SecRequirementCopyData(requirement, [], &data) == errSecSuccess, let data else { return nil }
        return data as Data
    }

    private struct TCCAccessRecord {
        let authValue: Int
        let csreq: Data?
    }

    /// Reads the Accessibility row for EV from the TCC database. The app has
    /// Full Disk Access, so the child `sqlite3` process can open the database
    /// exactly like it reads `~/Library/Messages/chat.db`. Fail-soft: any
    /// unreadable DB, schema change, or query error returns nil rather than
    /// breaking the panel.
    private static func tccAccessRecord(authValue: Int) -> TCCAccessRecord? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            // macOS 14 and earlier kept per-user grants here.
            "\(home)/Library/Application Support/com.apple.TCC/TCC.db",
            // macOS 15+ merges them into the system database.
            "/Library/Application Support/com.apple.TCC/TCC.db",
        ]
        guard let db = candidates.first(where: { FileManager.default.isReadableFile(atPath: $0) }) else {
            return nil
        }
        let query = "SELECT auth_value, hex(csreq) FROM access WHERE service = 'kTCCServiceAccessibility' AND client = 'com.ev.suit' AND auth_value = \(authValue) LIMIT 1;"
        let output = runSQLite(db, query) ?? ""
        let line = output.split(separator: "\n", omittingEmptySubsequences: true).first.map(String.init) ?? ""
        let parts = line.split(separator: "|", omittingEmptySubsequences: false).map(String.init)
        guard parts.count >= 2, let auth = Int(parts[0]) else { return nil }
        return TCCAccessRecord(
            authValue: auth,
            csreq: decodeHex(parts[1])
        )
    }

    private static func decodeHex(_ hex: String) -> Data? {
        let trimmed = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        var bytes = [UInt8]()
        bytes.reserveCapacity(trimmed.count / 2)
        var index = trimmed.startIndex
        while index < trimmed.endIndex {
            let end = trimmed.index(index, offsetBy: 2, limitedBy: trimmed.endIndex) ?? trimmed.endIndex
            guard end != index, let byte = UInt8(trimmed[index..<end], radix: 16) else { return nil }
            bytes.append(byte)
            index = end
        }
        return Data(bytes)
    }

    private static func runSQLite(_ dbPath: String, _ query: String) -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/sqlite3")
        task.arguments = ["-separator", "|", dbPath, query]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do { try task.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// Retained so `requestWhenInUseAuthorization()` can resolve instead of
    /// being deallocated mid-request (a thrown-away manager never prompts).
    private static let locationManager = CLLocationManager()

    /// Triggers the Automation (Apple Events) consent prompt by actually
    /// asking System Events to do something. This is more reliable than
    /// `AEDeterminePermissionToAutomateTarget`, which only prompts when the
    /// target app is already running; System Events is always available.
    private static func requestAutomationViaAppleScript() -> Bool {
        guard let script = NSAppleScript(
            source: "tell application \"System Events\" to count every process"
        ) else { return false }
        var error: NSDictionary?
        script.executeAndReturnError(&error)
        if let error {
            // -1743 ("not authorized") means the consent prompt WAS shown and
            // the user denied it — the request itself was still made.
            let code = (error[NSAppleScript.errorNumber] as? NSNumber)?.intValue ?? 0
            return code == -1743
        }
        return true
    }

    private static let automationTargets = [
        "com.apple.MobileSMS",
        "com.apple.mail",
        "com.apple.systemevents",
    ]

    private static func automationStatus() -> PermissionStatus {
        let states = automationTargets.map { automationState(for: $0) }
        let state: PermissionState
        if states.allSatisfy({ $0 == .granted }) {
            state = .granted
        } else if states.contains(.denied) || states.contains(.restricted) {
            state = .denied
        } else if states.contains(.granted) {
            state = .partial
        } else {
            state = .notDetermined
        }
        return PermissionStatus(
            kind: .automation,
            state: state,
            whatBreaks: "EV cannot control Messages, Mail, or System Events (send texts/email, read inboxes, drive apps).",
            settingsURL: settingsURL(for: .automation),
            canRequest: true
        )
    }

    private static func automationState(for bundleID: String) -> PermissionState {
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
            // TCC only answers for running targets, so a closed Messages/Mail
            // always reports "not determined" and the aggregate row would show
            // "partial" forever. Fall back to the last state observed while the
            // target was running (kept in sync on every live check).
            return automationPersistedState(for: bundleID) ?? .notDetermined
        }
        var pid = app.processIdentifier
        var descriptor = AEAddressDesc()
        let status = AECreateDesc(
            typeKernelProcessID,
            &pid,
            MemoryLayout<pid_t>.size,
            &descriptor
        )
        guard status == OSStatus(noErr) else { return automationPersistedState(for: bundleID) ?? .notDetermined }
        defer { AEDisposeDesc(&descriptor) }
        let permission = AEDeterminePermissionToAutomateTarget(
            &descriptor,
            typeWildCard,
            typeWildCard,
            false
        )
        let state: PermissionState
        switch permission {
        case OSStatus(noErr): state = .granted
        case OSStatus(errAEEventNotPermitted): state = .denied
        case OSStatus(errAEEventWouldRequireUserConsent): state = .notDetermined
        default: state = .notDetermined
        }
        persistAutomationState(state, for: bundleID)
        return state
    }

    /// Requests automation consent for every target. System Events is asked
    /// directly (always running); Messages and Mail are launched when closed
    /// so their consent prompt — and their row in the Automation pane — can
    /// actually appear. Targets we launched are quit again afterwards.
    /// Brings a menu-bar (LSUIElement) accessory to the foreground so macOS
    /// actually presents TCC consent dialogs. Background activation is weaker;
    /// `ignoringOtherApps` is deprecated but still the only form that reliably
    /// promotes an accessory app above its app switcher policy.
    private static func activateForPrompt() async {
        await MainActor.run {
            NSApp.activate(ignoringOtherApps: true)
            NSRunningApplication.current.activate(options: [.activateIgnoringOtherApps, .activateAllWindows])
        }
        try? await Task.sleep(nanoseconds: 400_000_000)
    }

    private static func requestAutomationAll() async -> Bool {
        // Consent dialogs from a menu-bar accessory present reliably only when
        // the process is active — activate before firing any of them.
        await activateForPrompt()
        await MainActor.run { _ = requestAutomationViaAppleScript() }
        var anyRequested = false
        for target in automationTargets {
            anyRequested = await requestAutomation(for: target) || anyRequested
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        return anyRequested
    }

    private static func requestAutomation(for bundleID: String) async -> Bool {
        let wasRunning = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first != nil
        var app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first
        if app == nil {
            // A non-running target cannot be prompted; launch it (briefly) so
            // macOS can present the per-target Automation consent dialog.
            if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
                _ = await MainActor.run { NSWorkspace.shared.open(url) }
                for _ in 0..<40 {
                    try? await Task.sleep(nanoseconds: 100_000_000)
                    if let launched = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first {
                        app = launched
                        break
                    }
                }
            }
        }
        guard let app else { return false }
        var pid = app.processIdentifier
        var descriptor = AEAddressDesc()
        let status = AECreateDesc(
            typeKernelProcessID,
            &pid,
            MemoryLayout<pid_t>.size,
            &descriptor
        )
        guard status == OSStatus(noErr) else { return false }
        defer { AEDisposeDesc(&descriptor) }
        let granted = AEDeterminePermissionToAutomateTarget(
            &descriptor,
            typeWildCard,
            typeWildCard,
            true
        ) == OSStatus(noErr)
        // The target is running right now, so refresh the persisted state.
        persistAutomationState(automationState(for: bundleID), for: bundleID)
        if !wasRunning {
            app.terminate()
        }
        return granted
    }

    private static func automationPersistedState(for bundleID: String) -> PermissionState? {
        guard let raw = UserDefaults.standard.string(forKey: "ev.automation.state.\(bundleID)") else {
            return nil
        }
        return PermissionState(rawValue: raw)
    }

    private static func persistAutomationState(_ state: PermissionState, for bundleID: String) {
        UserDefaults.standard.set(state.rawValue, forKey: "ev.automation.state.\(bundleID)")
    }

    private static func fullDiskAccessStatus() -> PermissionStatus {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let protectedPaths = [
            "\(home)/Library/Messages",
            "\(home)/Library/Mail",
        ]
        let state: PermissionState = protectedPaths.contains {
            FileManager.default.isReadableFile(atPath: $0)
        } ? .granted : .denied
        return PermissionStatus(
            kind: .fullDiskAccess,
            state: state,
            whatBreaks: "Messages and Mail data cannot be read, so EVLifeHelper messages.list/mail.list cannot work.",
            settingsURL: settingsURL(for: .fullDiskAccess),
            canRequest: false
        )
    }

    private static func contactsStatus() -> PermissionStatus {
        let state: PermissionState
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .contacts,
            state: state,
            whatBreaks: "EVLifeHelper contacts.list / contacts.resolve cannot return any contact data.",
            settingsURL: settingsURL(for: .contacts),
            canRequest: true
        )
    }

    private static func calendarsStatus() -> PermissionStatus {
        let state: PermissionState
        switch EKEventStore.authorizationStatus(for: .event) {
        case .fullAccess, .writeOnly: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .calendars,
            state: state,
            whatBreaks: "Calendar events cannot be read or created (Agent 12's calendar signals stay dark).",
            settingsURL: settingsURL(for: .calendars),
            canRequest: true
        )
    }

    private static func remindersStatus() -> PermissionStatus {
        let state: PermissionState
        switch EKEventStore.authorizationStatus(for: .reminder) {
        case .fullAccess, .writeOnly: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .reminders,
            state: state,
            whatBreaks: "Reminders cannot be read or created; routine follow-ups stay in EV memory only.",
            settingsURL: settingsURL(for: .reminders),
            canRequest: true
        )
    }

    private static func notificationStatus() async -> PermissionStatus {
        guard let center = ObjCException.notificationCenter() else {
            return PermissionStatus(
                kind: .notifications,
                state: .notDetermined,
                whatBreaks: "EV cannot surface alerts/digests delivered through Agent 14's notifier path.",
                settingsURL: settingsURL(for: .notifications),
                canRequest: true
            )
        }
        let settings = await center.notificationSettings()
        let state: PermissionState
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            state = .granted
        case .denied:
            state = .denied
        case .notDetermined:
            state = .notDetermined
        @unknown default:
            state = .notDetermined
        }
        return PermissionStatus(
            kind: .notifications,
            state: state,
            whatBreaks: "EV cannot surface alerts/digests delivered through Agent 14's notifier path.",
            settingsURL: settingsURL(for: .notifications),
            canRequest: true
        )
    }

    private static func bluetoothStatus() -> PermissionStatus {
        let state: PermissionState
        switch CBManager.authorization {
        case .allowedAlways: state = .granted
        case .denied, .restricted: state = .denied
        case .notDetermined: state = .notDetermined
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .bluetooth,
            state: state,
            whatBreaks: "Bluetooth peripherals (keyboard/mouse/health belt) cannot be discovered or used.",
            settingsURL: settingsURL(for: .bluetooth),
            canRequest: true
        )
    }

    private static func inputMonitoringStatus() -> PermissionStatus {
        let state: PermissionState
        switch IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) {
        case kIOHIDAccessTypeGranted: state = .granted
        case kIOHIDAccessTypeDenied: state = .denied
        case kIOHIDAccessTypeUnknown: state = .notDetermined
        default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .inputMonitoring,
            state: state,
            whatBreaks: "Low-level keyboard/mouse event monitoring (global hotkey fallback) cannot observe input.",
            settingsURL: settingsURL(for: .inputMonitoring),
            canRequest: true
        )
    }

    private static func locationStatus() -> PermissionStatus {
        let status = CLLocationManager().authorizationStatus
        let state: PermissionState
        switch status {
        case .authorizedAlways, .authorizedWhenInUse: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .location,
            state: state,
            whatBreaks: "Location-aware HUD cards (leave-by times, routes) cannot use your current position.",
            settingsURL: settingsURL(for: .location),
            canRequest: true
        )
    }
}

struct PermissionRowView: View {
    let status: PermissionStatus
    /// Called after a programmatic request finishes so the panel can reload
    /// with fresh live TCC state (a freshly granted permission flips quickly).
    var onRequest: (() async -> Void)? = nil

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(stateColor)
                .frame(width: 8, height: 8)
                .padding(.top, 5)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(status.kind.rawValue)
                        .font(.caption)
                        .fontWeight(.semibold)
                    Text(stateLabel)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Text(status.whatBreaks)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let repairHint = status.repairHint {
                    Text(repairHint)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 6) {
                    if showsAskButton {
                        Button(askButtonTitle) {
                            Task {
                                _ = await PermissionCenter.request(status.kind)
                                await onRequest?()
                            }
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                    }
                    Button("Open System Settings") {
                        PermissionCenter.openSettings(for: status.kind)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                }
                .padding(.top, 2)
            }
            Spacer()
        }
    }

    /// A programmatic request only helps while the OS will still show a
    /// prompt — that is, before the user has decided. The exception is Screen
    /// Recording, which has no "undecided" state: re-requesting re-registers
    /// EV in the pane even after a denial. Accessibility offers a re-grant
    /// path when a stale (signature-mismatched) record is detected.
    private var showsAskButton: Bool {
        switch status.state {
        case .notDetermined:
            return status.canRequest
        case .denied:
            return status.kind == .screenRecording
                || status.kind == .accessibility
        case .partial:
            return status.kind == .automation
        case .granted, .restricted:
            return false
        }
    }

    private var askButtonTitle: String {
        switch status.kind {
        case .accessibility: return "Enable Mac Control"
        default: return "Ask"
        }
    }

    private var stateLabel: String {
        switch status.state {
        case .granted: return "granted"
        case .denied: return "denied"
        case .notDetermined: return "not asked"
        case .restricted: return "restricted"
        case .partial: return "partial"
        }
    }

    private var stateColor: Color {
        switch status.state {
        case .granted: return .green
        case .denied: return .red
        case .notDetermined: return .orange
        case .restricted: return .red
        case .partial: return .orange
        }
    }
}

struct PermissionsPanelView: View {
    var onBack: (() -> Void)? = nil
    @State private var statuses: [PermissionStatus] = []
    @State private var isRequesting = false
    /// Version-keyed so a *new* build re-runs the registration sweep exactly
    /// once even if an older build already set the old key. Bump
    /// ``PermissionCenter.registrationVersion`` whenever the request set or
    /// order changes.
    @AppStorage("ev.permissions.autoRequestedVersion") private var autoRequestedVersion = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                if let onBack {
                    Button(action: onBack) {
                        Label("Back", systemImage: "chevron.left")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .keyboardShortcut(.cancelAction)
                }
                Text("Grant EVIE my life")
                    .font(.headline)
                Spacer()
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Refresh") {
                    Task { await load() }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                Button("Quit") {
                    AppLifecycle.quit()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
            Button {
                isRequesting = true
                Task {
                    statuses = await PermissionCenter.requestAll()
                    isRequesting = false
                }
            } label: {
                Label(
                    isRequesting ? "Answer the prompts…" : "Grant All — request every permission",
                    systemImage: "checkmark.shield"
                )
            }
            .disabled(isRequesting)
            .controlSize(.small)
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text(helpText)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Divider()
                    if statuses.isEmpty {
                        ProgressView()
                            .frame(maxWidth: .infinity, alignment: .center)
                            .padding(.vertical, 16)
                    } else {
                        ForEach(statuses, id: \.kind) { status in
                            PermissionRowView(status: status) {
                                await load()
                            }
                        }
                    }
                }
                .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: 520, alignment: .top)
        .onReceive(NotificationCenter.default.publisher(for: MicrophoneAuthorization.didChange)) { _ in
            Task { await load() }
        }
        .task {
            await load()
            // One-time per version: fire every still-undecided request so EV
            // registers in each System Settings privacy pane. macOS only
            // lists an app after it has asked for the permission, so without
            // this the panes stay empty even though EV is "installed". The
            // flag is written only after the sweep completes, so a cancelled
            // sweep (panel closed mid-way) re-runs on the next open.
            if autoRequestedVersion != PermissionCenter.registrationVersion {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard !Task.isCancelled else { return }
                isRequesting = true
                statuses = await PermissionCenter.requestPending()
                isRequesting = false
                autoRequestedVersion = PermissionCenter.registrationVersion
            }
        }
        .onReceive(Timer.publish(every: 2, on: .main, in: .common).autoconnect()) { _ in
            Task { await load() }
        }
    }

    private var helpText: String {
        "macOS only lists an app in a privacy pane after it has asked for the permission, so this fires one consent prompt per permission — answer each, then toggle anything still off in System Settings. Accessibility and Full Disk Access have no prompt: add EV with their \"+\" button. If Accessibility stays denied after granting, remove EV from the list and toggle it back on. Grants stick to the code signature, so keep the same build while toggling."
    }

    private var summary: String {
        let granted = statuses.filter { $0.state == .granted }.count
        return "\(granted)/\(statuses.count) granted"
    }

    private func load() async {
        statuses = await PermissionCenter.statuses()
    }
}
