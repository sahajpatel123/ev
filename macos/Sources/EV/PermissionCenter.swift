import AppKit
import ApplicationServices
import AVFoundation
import Contacts
import CoreBluetooth
import CoreGraphics
import CoreLocation
import Darwin
import EventKit
import Foundation
import Security
import Speech
import UserNotifications

enum PermissionKind: String, CaseIterable, Identifiable {
    case microphone
    case speechRecognition
    case camera
    case screenRecording
    case automation
    case contacts
    case calendars
    case reminders
    case notifications
    case bluetooth
    case inputMonitoring
    case accessibility
    case location
    case fullDiskAccess

    var id: String { rawValue }

    /// Title matching the System Settings Privacy pane the user is looking at.
    var title: String {
        switch self {
        case .microphone: return "Microphone"
        case .speechRecognition: return "Speech Recognition"
        case .camera: return "Camera"
        case .screenRecording: return "Screen Recording"
        case .automation: return "Automation"
        case .contacts: return "Contacts"
        case .calendars: return "Calendars"
        case .reminders: return "Reminders"
        case .notifications: return "Notifications"
        case .bluetooth: return "Bluetooth"
        case .inputMonitoring: return "Input Monitoring"
        case .accessibility: return "Accessibility"
        case .location: return "Location"
        case .fullDiskAccess: return "Full Disk Access"
        }
    }

    /// `tccutil` service name. Notifications are stored by usernoted, not TCC.
    var tccService: String? {
        switch self {
        case .microphone: return "Microphone"
        case .speechRecognition: return "SpeechRecognition"
        case .camera: return "Camera"
        case .screenRecording: return "ScreenCapture"
        case .automation: return "AppleEvents"
        case .contacts: return "AddressBook"
        case .calendars: return "Calendar"
        case .reminders: return "Reminders"
        case .notifications: return nil
        case .bluetooth: return "BluetoothAlways"
        case .inputMonitoring: return "ListenEvent"
        case .accessibility: return "Accessibility"
        case .location: return "Location"
        case .fullDiskAccess: return "SystemPolicyAllFiles"
        }
    }

    /// Info.plist keys macOS requires before it will show a prompt. Without the
    /// string the process is killed instead of prompted, so the app never
    /// reaches the Privacy pane at all.
    var usageDescriptionKeys: [String] {
        switch self {
        case .microphone: return ["NSMicrophoneUsageDescription"]
        case .speechRecognition: return ["NSSpeechRecognitionUsageDescription"]
        case .camera: return ["NSCameraUsageDescription"]
        case .screenRecording: return ["NSScreenCaptureUsageDescription"]
        case .automation: return ["NSAppleEventsUsageDescription"]
        case .contacts: return ["NSContactsUsageDescription"]
        case .calendars: return ["NSCalendarsUsageDescription", "NSCalendarsFullAccessUsageDescription"]
        case .reminders: return ["NSRemindersUsageDescription", "NSRemindersFullAccessUsageDescription"]
        case .bluetooth: return ["NSBluetoothAlwaysUsageDescription"]
        case .location: return ["NSLocationWhenInUseUsageDescription"]
        case .notifications, .inputMonitoring, .accessibility, .fullDiskAccess:
            return []
        }
    }

    /// Anchor on the Privacy & Security settings extension. Notifications use
    /// a different extension and are handled in ``PermissionCenter.settingsURL``.
    var privacyAnchor: String? {
        switch self {
        case .microphone: return "Privacy_Microphone"
        case .speechRecognition: return "Privacy_SpeechRecognition"
        case .camera: return "Privacy_Camera"
        case .screenRecording: return "Privacy_ScreenCapture"
        case .automation: return "Privacy_Automation"
        case .contacts: return "Privacy_Contacts"
        case .calendars: return "Privacy_Calendars"
        case .reminders: return "Privacy_Reminders"
        case .notifications: return nil
        case .bluetooth: return "Privacy_Bluetooth"
        case .inputMonitoring: return "Privacy_ListenEvent"
        case .accessibility: return "Privacy_Accessibility"
        case .location: return "Privacy_LocationServices"
        case .fullDiskAccess: return "Privacy_AllFiles"
        }
    }

    /// Full Disk Access has no request API — the user must add EV.app with +.
    var canRequestProgrammatically: Bool {
        self != .fullDiskAccess
    }
}

enum PermissionState: String {
    case granted
    case denied
    case notDetermined
    case restricted
}

struct PermissionStatus {
    let kind: PermissionKind
    let state: PermissionState
    let whatBreaks: String
    let settingsURL: URL?

    /// macOS never re-prompts once the user has refused: the app stays in the
    /// Privacy list with its switch off. Resetting the TCC decision is the
    /// only way to get the prompt back.
    var resetCommand: String? {
        guard state == .denied || state == .restricted, let service = kind.tccService else { return nil }
        return "tccutil reset \(service) \(PermissionCenter.bundleIdentifier)"
    }
}

/// One human-readable fact about why EV may be missing from a Privacy pane.
struct PermissionFact: Identifiable {
    let title: String
    let detail: String
    let ok: Bool
    let fix: String?

    var id: String { title }
}

/// Detects every permission SUIT can need, explains what breaks when denied,
/// and deep-links to the exact System Settings pane. No silent failures.
///
/// Detection alone is not enough: macOS only lists an app in a Privacy pane
/// after that app has triggered a TCC request (or been added with +). Reading
/// `authorizationStatus` registers nothing, which is why an app that only ever
/// checks its state is invisible in System Settings. The `request*` functions
/// below are the ones that make EV appear.
///
/// EV is an `LSUIElement` accessory app. Permission sheets for accessory apps
/// are frequently created and then discarded because EV is not the active
/// application, and a discarded sheet writes **no** TCC row. Every request
/// therefore runs inside ``AppForeground.withActivation``.
@MainActor
enum PermissionCenter {
    static var bundleIdentifier: String {
        Bundle.main.bundleIdentifier ?? "com.ev.suit"
    }

    static func statuses() async -> [PermissionStatus] {
        PermissionBrokers.shared.notificationSnapshot = await notificationState()
        return PermissionKind.allCases.map { kind in
            PermissionStatus(
                kind: kind,
                state: currentState(for: kind),
                whatBreaks: whatBreaks(for: kind),
                settingsURL: settingsURL(for: kind)
            )
        }
    }

    // MARK: - Requests

    /// Trigger every TCC request in one pass so a single click registers EV in
    /// every Privacy pane the user listed. Requests run one at a time — parallel
    /// prompts cancel each other — and Settings is **not** opened here. Opening
    /// a pane before the matching request has landed is how the list looks empty.
    @discardableResult
    static func requestAll() async -> [PermissionStatus] {
        await AppForeground.withActivation {
            for kind in PermissionKind.allCases where kind.canRequestProgrammatically {
                _ = await requestWithoutOpeningSettings(kind)
                try? await Task.sleep(nanoseconds: 280_000_000)
            }
            // Screen Recording and Bluetooth sheets are asynchronous; give them
            // a moment to attach before we drop back to accessory.
            try? await Task.sleep(nanoseconds: 800_000_000)
            return await statuses()
        }
    }

    /// Request one permission. Already-denied services cannot be re-prompted,
    /// so their pane is opened after the request so the user can flip the switch.
    /// Full Disk Access has no prompt: Finder is revealed so the user can drag
    /// EV.app onto the + button.
    @discardableResult
    static func request(_ kind: PermissionKind) async -> PermissionState {
        await AppForeground.withActivation {
            if kind == .fullDiskAccess {
                revealAppInFinder()
                openSettings(for: .fullDiskAccess)
                return currentState(for: .fullDiskAccess)
            }
            let state = await requestWithoutOpeningSettings(kind)
            if state == .denied || state == .restricted {
                openSettings(for: kind)
            }
            return state
        }
    }

    static func revealAppInFinder() {
        NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL])
    }

    // MARK: - Per-service requests

    /// Real TCC request. This — not `authorizationStatus` — is what puts EV
    /// into System Settings > Privacy & Security > Microphone.
    @discardableResult
    static func requestMicrophone() async -> PermissionState {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        return currentState(for: .microphone)
    }

    @discardableResult
    static func requestSpeechRecognition() async -> PermissionState {
        if SFSpeechRecognizer.authorizationStatus() == .notDetermined {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                SFSpeechRecognizer.requestAuthorization { _ in
                    continuation.resume()
                }
            }
        }
        return currentState(for: .speechRecognition)
    }

    @discardableResult
    static func requestCamera() async -> PermissionState {
        if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .video)
        }
        return currentState(for: .camera)
    }

    /// `CGRequestScreenCaptureAccess` is the call that adds EV to Screen &
    /// System Audio Recording. Preflight returning false means either "never
    /// asked" or "denied" — both need this call or the pane stays empty.
    /// The function returns the grant *before* the user answers the async
    /// prompt, so a false return is not treated as a final denial.
    @discardableResult
    static func requestScreenRecording() async -> PermissionState {
        if CGPreflightScreenCaptureAccess() {
            return .granted
        }
        PermissionBrokers.shared.didAskScreenRecording = true
        _ = CGRequestScreenCaptureAccess()
        try? await Task.sleep(nanoseconds: 400_000_000)
        return currentState(for: .screenRecording)
    }

    @discardableResult
    static func requestAutomation() async -> PermissionState {
        PermissionBrokers.shared.didAskAutomation = true
        await ensureSystemEventsRunning()
        _ = appleEventsPermission(bundleIdentifier: "com.apple.finder", prompt: true)
        _ = appleEventsPermission(bundleIdentifier: "com.apple.systemevents", prompt: true)
        return currentState(for: .automation)
    }

    @discardableResult
    static func requestContacts() async -> PermissionState {
        if CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
            _ = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                CNContactStore().requestAccess(for: .contacts) { granted, _ in
                    continuation.resume(returning: granted)
                }
            }
        }
        return currentState(for: .contacts)
    }

    @discardableResult
    static func requestCalendars() async -> PermissionState {
        let store = EKEventStore()
        if calendarState(from: EKEventStore.authorizationStatus(for: .event)) == .notDetermined {
            _ = try? await store.requestFullAccessToEvents()
        }
        return currentState(for: .calendars)
    }

    @discardableResult
    static func requestReminders() async -> PermissionState {
        let store = EKEventStore()
        if calendarState(from: EKEventStore.authorizationStatus(for: .reminder)) == .notDetermined {
            _ = try? await store.requestFullAccessToReminders()
        }
        return currentState(for: .reminders)
    }

    @discardableResult
    static func requestNotifications() async -> PermissionState {
        let center = UNUserNotificationCenter.current()
        _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
        let state = await notificationState()
        PermissionBrokers.shared.notificationSnapshot = state
        return state
    }

    /// Creating a `CBCentralManager` is the Bluetooth TCC request. The manager
    /// has to stay alive: releasing it before the user answers cancels the
    /// prompt and writes no row.
    @discardableResult
    static func requestBluetooth() async -> PermissionState {
        await PermissionBrokers.shared.ensureBluetoothManager()
        return currentState(for: .bluetooth)
    }

    @discardableResult
    static func requestInputMonitoring() async -> PermissionState {
        if IOHIDTCC.check() == IOHIDTCC.granted {
            return .granted
        }
        PermissionBrokers.shared.didAskInputMonitoring = true
        _ = IOHIDTCC.request()
        try? await Task.sleep(nanoseconds: 300_000_000)
        return currentState(for: .inputMonitoring)
    }

    /// The prompt option is what registers EV in the Accessibility list; the
    /// return value is the trust state before the user answers.
    @discardableResult
    static func requestAccessibility() async -> PermissionState {
        if AXIsProcessTrusted() {
            return .granted
        }
        PermissionBrokers.shared.didAskAccessibility = true
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
        try? await Task.sleep(nanoseconds: 300_000_000)
        return currentState(for: .accessibility)
    }

    @discardableResult
    static func requestLocation() async -> PermissionState {
        await PermissionBrokers.shared.requestWhenInUseLocation()
        return currentState(for: .location)
    }

    // MARK: - System Settings deep links

    static func openSettings(for kind: PermissionKind) {
        guard let url = settingsURL(for: kind) else { return }
        NSWorkspace.shared.open(url)
    }

    /// On macOS 13+ the Privacy panes live in the settings extension
    /// (`com.apple.settings.PrivacySecurity.extension?Privacy_Microphone`); the
    /// legacy `com.apple.preference.security?Privacy_Microphone` pane id is
    /// still routed by System Settings but is the older spelling, so it is only
    /// used below 13. The URL *scheme* stays `x-apple.systempreferences` on
    /// every version — `x-apple.systemsettings` has no handler.
    static func settingsURL(for kind: PermissionKind) -> URL? {
        if kind == .notifications {
            if usesSettingsExtensionScheme {
                return URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=\(bundleIdentifier)")
            }
            return URL(string: "x-apple.systempreferences:com.apple.preference.notifications")
        }
        guard let anchor = kind.privacyAnchor else { return nil }
        if usesSettingsExtensionScheme {
            return URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?\(anchor)")
        }
        return URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)")
    }

    private static var usesSettingsExtensionScheme: Bool {
        ProcessInfo.processInfo.isOperatingSystemAtLeast(
            OperatingSystemVersion(majorVersion: 13, minorVersion: 0, patchVersion: 0)
        )
    }

    // MARK: - Diagnostics

    /// Facts that answer "why is EV not in the Privacy list?". Each one is a
    /// real cause seen in the wild, with the concrete fix next to it.
    static func diagnostics() -> [PermissionFact] {
        var facts: [PermissionFact] = []
        let bundlePath = Bundle.main.bundleURL.path

        facts.append(installLocationFact(bundlePath: bundlePath))
        facts.append(translocationFact(bundlePath: bundlePath))
        facts.append(quarantineFact(bundlePath: bundlePath))
        facts.append(signatureFact())
        facts.append(bundleIdentifierFact())
        facts.append(activationPolicyFact())
        facts.append(contentsOf: usageDescriptionFacts())
        return facts
    }

    private static func installLocationFact(bundlePath: String) -> PermissionFact {
        let userApplications = (NSHomeDirectory() as NSString).appendingPathComponent("Applications")
        let installed = bundlePath.hasPrefix("/Applications/") || bundlePath.hasPrefix(userApplications + "/")
        return PermissionFact(
            title: "Install location",
            detail: bundlePath,
            ok: installed,
            fix: installed
                ? nil
                : "Run ./scripts/install.sh (or drag EV.app into /Applications) and launch it from there. TCC grants follow the app's identity and location; a copy in build/ is treated as a different, throwaway app."
        )
    }

    private static func translocationFact(bundlePath: String) -> PermissionFact {
        // Gatekeeper path randomisation runs a quarantined app from a random
        // read-only mount such as
        // /private/var/folders/<x>/T/AppTranslocation/<UUID>/d/EV.app. The
        // identity changes on every launch, so nothing granted ever sticks.
        let translocated = bundlePath.contains("/AppTranslocation/")
            || bundlePath.hasPrefix("/private/var/folders/")
        return PermissionFact(
            title: "App Translocation",
            detail: translocated ? "running from a randomised read-only path" : "not translocated",
            ok: !translocated,
            fix: translocated
                ? "Quit EV, run xattr -dr com.apple.quarantine on the bundle, move it to /Applications with Finder, then relaunch. While translocated every launch looks like a new app to TCC."
                : nil
        )
    }

    private static func quarantineFact(bundlePath: String) -> PermissionFact {
        let quarantined = hasQuarantineAttribute(atPath: bundlePath)
        return PermissionFact(
            title: "Quarantine flag",
            detail: quarantined ? "com.apple.quarantine is set" : "clear",
            ok: !quarantined,
            fix: quarantined
                ? "Run xattr -dr com.apple.quarantine \"\(bundlePath)\". The quarantine attribute is what triggers App Translocation."
                : nil
        )
    }

    private static func signatureFact() -> PermissionFact {
        let info = signingInformation()
        guard info.signed else {
            return PermissionFact(
                title: "Code signature",
                detail: "unsigned",
                ok: false,
                fix: "Re-run ./scripts/package.sh. TCC keys its records on the code signature; an unsigned bundle cannot hold a grant."
            )
        }
        let identifier = info.identifier ?? "unknown"
        if info.adHoc {
            return PermissionFact(
                title: "Code signature",
                detail: "ad-hoc, identifier \(identifier)",
                ok: false,
                fix: "Ad-hoc signatures identify the app by its cdhash, which changes on every rebuild, so grants stop applying after each package run. Set EV_CODESIGN_IDENTITY to a Developer ID or self-signed identity before ./scripts/package.sh for a stable identity."
            )
        }
        return PermissionFact(
            title: "Code signature",
            detail: "signed with a stable identity, identifier \(identifier)",
            ok: true,
            fix: nil
        )
    }

    private static func bundleIdentifierFact() -> PermissionFact {
        let identifier = Bundle.main.bundleIdentifier
        return PermissionFact(
            title: "Bundle identifier",
            detail: identifier ?? "missing",
            ok: identifier != nil,
            fix: identifier == nil
                ? "The binary is running outside an app bundle (for example straight from .build/release). TCC has nothing to attribute a grant to; launch /Applications/EV.app instead."
                : nil
        )
    }

    private static func activationPolicyFact() -> PermissionFact {
        let accessory = NSApp.activationPolicy() == .accessory
        return PermissionFact(
            title: "Activation policy",
            detail: accessory ? "accessory (menu bar)" : "regular (foreground)",
            ok: true,
            fix: "EV is a menu-bar app. Grant permissions temporarily brings it to the foreground so macOS will show each privacy dialog and write a TCC row. Opening a Privacy pane before that request lands shows an empty list."
        )
    }

    private static func usageDescriptionFacts() -> [PermissionFact] {
        var seen = Set<String>()
        var keys: [String] = []
        for kind in PermissionKind.allCases {
            for key in kind.usageDescriptionKeys where seen.insert(key).inserted {
                keys.append(key)
            }
        }
        return keys.map { key in
            let value = Bundle.main.object(forInfoDictionaryKey: key) as? String
            let present = !(value ?? "").isEmpty
            return PermissionFact(
                title: key,
                detail: present ? "present" : "missing",
                ok: present,
                fix: present
                    ? nil
                    : "macOS terminates a process that triggers this permission without a usage string, so no prompt appears and the app never reaches the Privacy list. Add \(key) to Resources/Info.plist and repackage."
            )
        }
    }

    /// `getxattr` returns the attribute length, or -1 with errno set when the
    /// attribute (or the path) is absent. Any failure means "not quarantined".
    private static func hasQuarantineAttribute(atPath path: String) -> Bool {
        path.withCString { cPath in
            getxattr(cPath, "com.apple.quarantine", nil, 0, 0, 0) > 0
        }
    }

    /// Signing identity of the *running* code, read through the Security
    /// framework so no subprocess is needed. `kSecCodeInfoIdentifier` is absent
    /// for unsigned code; ad-hoc code carries the adhoc flag and no certificate
    /// chain.
    private static func signingInformation() -> (signed: Bool, adHoc: Bool, identifier: String?) {
        var code: SecCode?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code else {
            return (false, false, nil)
        }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode else {
            return (false, false, nil)
        }
        var information: CFDictionary?
        let status = SecCodeCopySigningInformation(
            staticCode,
            SecCSFlags(rawValue: kSecCSSigningInformation),
            &information
        )
        guard status == errSecSuccess, let dictionary = information as NSDictionary? else {
            return (false, false, nil)
        }
        guard let identifier = dictionary[kSecCodeInfoIdentifier as String] as? String else {
            return (false, false, nil)
        }
        let adHocFlag: UInt32 = 0x0002 // kSecCodeSignatureAdhoc
        let flags = (dictionary[kSecCodeInfoFlags as String] as? NSNumber)?.uint32Value ?? 0
        let certificates = dictionary[kSecCodeInfoCertificates as String] as? [Any]
        let adHoc = (flags & adHocFlag) != 0 || (certificates?.isEmpty ?? true)
        return (true, adHoc, identifier)
    }

    // MARK: - Detection

    private static func requestWithoutOpeningSettings(_ kind: PermissionKind) async -> PermissionState {
        switch kind {
        case .microphone: return await requestMicrophone()
        case .speechRecognition: return await requestSpeechRecognition()
        case .camera: return await requestCamera()
        case .screenRecording: return await requestScreenRecording()
        case .automation: return await requestAutomation()
        case .contacts: return await requestContacts()
        case .calendars: return await requestCalendars()
        case .reminders: return await requestReminders()
        case .notifications: return await requestNotifications()
        case .bluetooth: return await requestBluetooth()
        case .inputMonitoring: return await requestInputMonitoring()
        case .accessibility: return await requestAccessibility()
        case .location: return await requestLocation()
        case .fullDiskAccess: return currentState(for: .fullDiskAccess)
        }
    }

    private static func currentState(for kind: PermissionKind) -> PermissionState {
        switch kind {
        case .microphone:
            return avState(AVCaptureDevice.authorizationStatus(for: .audio))
        case .speechRecognition:
            return speechState(SFSpeechRecognizer.authorizationStatus())
        case .camera:
            return avState(AVCaptureDevice.authorizationStatus(for: .video))
        case .screenRecording:
            if CGPreflightScreenCaptureAccess() { return .granted }
            return PermissionBrokers.shared.didAskScreenRecording ? .denied : .notDetermined
        case .automation:
            return automationState()
        case .contacts:
            return contactsState(CNContactStore.authorizationStatus(for: .contacts))
        case .calendars:
            return calendarState(from: EKEventStore.authorizationStatus(for: .event))
        case .reminders:
            return calendarState(from: EKEventStore.authorizationStatus(for: .reminder))
        case .notifications:
            return PermissionBrokers.shared.notificationSnapshot
        case .bluetooth:
            return bluetoothState(CBCentralManager.authorization)
        case .inputMonitoring:
            switch IOHIDTCC.check() {
            case IOHIDTCC.granted: return .granted
            case IOHIDTCC.denied: return .denied
            default:
                return PermissionBrokers.shared.didAskInputMonitoring ? .denied : .notDetermined
            }
        case .accessibility:
            if AXIsProcessTrusted() { return .granted }
            return PermissionBrokers.shared.didAskAccessibility ? .denied : .notDetermined
        case .location:
            return locationState(PermissionBrokers.shared.locationAuthorization)
        case .fullDiskAccess:
            return hasFullDiskAccess() ? .granted : .notDetermined
        }
    }

    private static func avState(_ status: AVAuthorizationStatus) -> PermissionState {
        switch status {
        case .authorized: return .granted
        case .denied: return .denied
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        @unknown default: return .notDetermined
        }
    }

    private static func speechState(_ status: SFSpeechRecognizerAuthorizationStatus) -> PermissionState {
        switch status {
        case .authorized: return .granted
        case .denied: return .denied
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        @unknown default: return .notDetermined
        }
    }

    private static func contactsState(_ status: CNAuthorizationStatus) -> PermissionState {
        switch status {
        case .authorized: return .granted
        case .denied: return .denied
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        @unknown default: return .notDetermined
        }
    }

    private static func calendarState(from status: EKAuthorizationStatus) -> PermissionState {
        switch status {
        case .denied:
            return .denied
        case .notDetermined:
            return .notDetermined
        case .restricted:
            return .restricted
        default:
            // `.fullAccess`, `.writeOnly`, and the deprecated `.authorized` alias
            // share this path so a duplicate-case compile error cannot happen
            // when two names have the same raw value.
            return .granted
        }
    }

    private static func bluetoothState(_ status: CBManagerAuthorization) -> PermissionState {
        switch status {
        case .allowedAlways: return .granted
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .notDetermined
        }
    }

    private static func locationState(_ status: CLAuthorizationStatus) -> PermissionState {
        switch status {
        case .authorizedAlways, .authorizedWhenInUse:
            return .granted
        case .denied:
            return .denied
        case .notDetermined:
            return .notDetermined
        case .restricted:
            return .restricted
        @unknown default:
            return .notDetermined
        }
    }

    private static func notificationState() async -> PermissionState {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            return .granted
        case .denied:
            return .denied
        case .notDetermined:
            return .notDetermined
        @unknown default:
            return .notDetermined
        }
    }

    private static func automationState() -> PermissionState {
        let finder = appleEventsPermission(bundleIdentifier: "com.apple.finder", prompt: false)
        let events = appleEventsPermission(bundleIdentifier: "com.apple.systemevents", prompt: false)
        if finder == noErr || events == noErr {
            return .granted
        }
        if finder == AppleEventTCC.notPermitted || events == AppleEventTCC.notPermitted {
            return .denied
        }
        if finder == AppleEventTCC.wouldRequireConsent || events == AppleEventTCC.wouldRequireConsent {
            return .notDetermined
        }
        return PermissionBrokers.shared.didAskAutomation ? .denied : .notDetermined
    }

    /// Opening the user TCC database is the usual Full Disk Access probe: the
    /// file exists for everyone, but only FDA lets another app read it.
    private static func hasFullDiskAccess() -> Bool {
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/com.apple.TCC/TCC.db")
        let fd = open(path, O_RDONLY)
        if fd >= 0 {
            close(fd)
            return true
        }
        return false
    }

    private static func appleEventsPermission(bundleIdentifier: String, prompt: Bool) -> OSStatus {
        let target = NSAppleEventDescriptor(bundleIdentifier: bundleIdentifier)
        var address = target.aeDesc
        return withUnsafePointer(to: &address) { pointer in
            AEDeterminePermissionToAutomateTarget(
                pointer,
                AEEventClass(typeWildCard),
                AEEventID(typeWildCard),
                prompt
            )
        }
    }

    private static func ensureSystemEventsRunning() async {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.systemevents")
        guard running.isEmpty else { return }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.systemevents") else {
            return
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.addsToRecentItems = false
        _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: configuration)
        try? await Task.sleep(nanoseconds: 500_000_000)
    }

    private static func whatBreaks(for kind: PermissionKind) -> String {
        switch kind {
        case .microphone:
            return "EV cannot hear the wake word “EVIE”, and the hotkey and Talk button cannot record."
        case .speechRecognition:
            return "On-device speech recognition prompts cannot complete, so dictation-style fallbacks stay unavailable."
        case .camera:
            return "Camera capture (share sheet / photo events) cannot run. Text and file capture still work."
        case .screenRecording:
            return "ScreenCaptureKit-based ambient context cannot see the screen."
        case .automation:
            return "EV cannot drive Finder or System Events when an action you asked for has to click another app."
        case .contacts:
            return "EV cannot look up people in Contacts when you ask it to remember or find someone."
        case .calendars:
            return "EV cannot read or add calendar events when you ask about your schedule."
        case .reminders:
            return "EV cannot read or add reminders when you ask it to track a task."
        case .notifications:
            return "EV cannot surface alerts and digests through the notifier path."
        case .bluetooth:
            return "EV cannot reach Bluetooth accessories such as a headset or speaker."
        case .inputMonitoring:
            return "HID-level input monitoring stays off. The ⇧⌘E hotkey still uses Accessibility."
        case .accessibility:
            return "The global hotkey cannot see key presses from other apps, so ⇧⌘E will not open the mic from anywhere."
        case .location:
            return "Place context (“what happened near me”) cannot run."
        case .fullDiskAccess:
            return "Collectors that read other apps’ files cannot run. There is no prompt: add EV.app with + in this pane."
        }
    }
}

// MARK: - IOHID (Input Monitoring) and Apple Events TCC codes

/// `errAEEventNotPermitted` / `errAEEventWouldRequireUserConsent` from
/// `AE/AppleEvents.h`. Named locally so a missing Swift overlay cannot
/// break the build.
private enum AppleEventTCC {
    static let notPermitted: OSStatus = -1743
    static let wouldRequireConsent: OSStatus = -1744
}

/// Input Monitoring has no Swift overlay. These are the IOKit entry points
/// `IOHIDCheckAccess` / `IOHIDRequestAccess` for `kIOHIDRequestTypeListenEvent`.
enum IOHIDTCC {
    static let listenEvent: UInt32 = 1
    static let granted: UInt32 = 0
    static let denied: UInt32 = 1
    static let unknown: UInt32 = 2

    static func check() -> UInt32 {
        IOHIDCheckAccess(listenEvent)
    }

    static func request() -> Bool {
        IOHIDRequestAccess(listenEvent)
    }
}

@_silgen_name("IOHIDCheckAccess")
private func IOHIDCheckAccess(_ requestType: UInt32) -> UInt32

@_silgen_name("IOHIDRequestAccess")
private func IOHIDRequestAccess(_ requestType: UInt32) -> Bool

// MARK: - Long-lived TCC brokers

/// Bluetooth and Location prompts are cancelled if their manager is released
/// before the user answers. Keep both for the life of the process after the
/// first request so the TCC row actually lands.
final class PermissionBrokers: NSObject, CBCentralManagerDelegate, CLLocationManagerDelegate {
    static let shared = PermissionBrokers()

    var didAskScreenRecording = false
    var didAskAccessibility = false
    var didAskInputMonitoring = false
    var didAskAutomation = false
    var notificationSnapshot: PermissionState = .notDetermined

    private var bluetoothManager: CBCentralManager?
    private var bluetoothWaiters: [CheckedContinuation<Void, Never>] = []
    private var locationManager: CLLocationManager?
    private var locationWaiters: [CheckedContinuation<Void, Never>] = []

    var locationAuthorization: CLAuthorizationStatus {
        if let locationManager {
            return locationManager.authorizationStatus
        }
        return CLLocationManager().authorizationStatus
    }

    func ensureBluetoothManager() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            bluetoothWaiters.append(continuation)
            if bluetoothManager == nil {
                bluetoothManager = CBCentralManager(
                    delegate: self,
                    queue: .main,
                    options: [CBCentralManagerOptionShowPowerAlertKey: false]
                )
            } else {
                finishBluetoothWait()
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
                self?.finishBluetoothWait()
            }
        }
    }

    func requestWhenInUseLocation() async {
        if locationManager == nil {
            let manager = CLLocationManager()
            manager.delegate = self
            locationManager = manager
        }
        let status = locationManager?.authorizationStatus ?? .notDetermined
        guard status == .notDetermined else { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            locationWaiters.append(continuation)
            locationManager?.requestWhenInUseAuthorization()
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in
                self?.finishLocationWait()
            }
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        finishBluetoothWait()
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        finishLocationWait()
    }

    private func finishBluetoothWait() {
        let waiters = bluetoothWaiters
        bluetoothWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    private func finishLocationWait() {
        let waiters = locationWaiters
        locationWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }
}
