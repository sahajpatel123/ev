import AppKit
import ApplicationServices
import AVFoundation
import CoreGraphics
import Darwin
import Foundation
import Security
import SwiftUI
import UserNotifications

enum PermissionKind: String, CaseIterable, Identifiable {
    case microphone
    case camera
    case screenRecording
    case notifications
    case accessibility

    var id: String { rawValue }

    /// `tccutil` service name for this permission. Notifications are stored by
    /// usernoted, not TCC, so there is nothing tccutil can reset for them.
    var tccService: String? {
        switch self {
        case .microphone: return "Microphone"
        case .camera: return "Camera"
        case .screenRecording: return "ScreenCapture"
        case .notifications: return nil
        case .accessibility: return "Accessibility"
        }
    }

    /// Info.plist key macOS requires before it will show a prompt. Without the
    /// string the process is killed instead of prompted, so the app never
    /// reaches the Privacy pane at all.
    var usageDescriptionKey: String? {
        switch self {
        case .microphone: return "NSMicrophoneUsageDescription"
        case .camera: return "NSCameraUsageDescription"
        case .screenRecording: return "NSScreenCaptureUsageDescription"
        case .notifications, .accessibility: return nil
        }
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
/// after that app has triggered a TCC request (or been granted). Reading
/// `authorizationStatus` registers nothing, which is why an app that only ever
/// checks its state is invisible in System Settings. The `request*` functions
/// below are the ones that make EV appear.
enum PermissionCenter {
    static var bundleIdentifier: String {
        Bundle.main.bundleIdentifier ?? "com.ev.suit"
    }

    static func statuses() async -> [PermissionStatus] {
        var result: [PermissionStatus] = []
        result.append(microphoneStatus())
        result.append(cameraStatus())
        result.append(screenRecordingStatus())
        result.append(await notificationStatus())
        result.append(accessibilityStatus())
        return result
    }

    // MARK: - Requests

    /// Trigger every TCC request in one pass so a single click registers EV in
    /// every Privacy pane. Microphone runs first (it is the permission EV
    /// actually needs to work) and accessibility last, because its prompt is a
    /// modal alert that steals focus.
    ///
    /// Already-denied permissions cannot be re-prompted; the first one opens
    /// System Settings so the user lands on a pane where EV is listed with its
    /// switch off. Only one pane is opened — five settings windows would be
    /// worse than none.
    @discardableResult
    static func requestAll() async -> [PermissionStatus] {
        var deniedKind: PermissionKind?

        if await requestMicrophone() == .denied { deniedKind = deniedKind ?? .microphone }
        if await requestCamera() == .denied { deniedKind = deniedKind ?? .camera }
        if requestScreenRecording() == .denied { deniedKind = deniedKind ?? .screenRecording }
        if await requestNotifications() == .denied { deniedKind = deniedKind ?? .notifications }
        if requestAccessibility() == .denied { deniedKind = deniedKind ?? .accessibility }

        if let deniedKind {
            openSettings(for: deniedKind)
        }
        return await statuses()
    }

    /// Request one permission, and open its pane when the user already refused
    /// it (no prompt will ever appear again for a denied service).
    @discardableResult
    static func request(_ kind: PermissionKind) async -> PermissionState {
        let state: PermissionState
        switch kind {
        case .microphone: state = await requestMicrophone()
        case .camera: state = await requestCamera()
        case .screenRecording: state = requestScreenRecording()
        case .notifications: state = await requestNotifications()
        case .accessibility: state = requestAccessibility()
        }
        if state == .denied || state == .restricted {
            openSettings(for: kind)
        }
        return state
    }

    /// Real TCC request. This — not `authorizationStatus` — is what puts EV
    /// into System Settings > Privacy & Security > Microphone.
    @discardableResult
    static func requestMicrophone() async -> PermissionState {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        return state(for: AVCaptureDevice.authorizationStatus(for: .audio))
    }

    @discardableResult
    static func requestCamera() async -> PermissionState {
        if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .video)
        }
        return state(for: AVCaptureDevice.authorizationStatus(for: .video))
    }

    /// `CGRequestScreenCaptureAccess` prompts and adds EV to the Screen & System
    /// Audio Recording list. It returns the current grant, not the answer to the
    /// prompt: the prompt is asynchronous and only shown once per app session.
    @discardableResult
    static func requestScreenRecording() -> PermissionState {
        if CGPreflightScreenCaptureAccess() { return .granted }
        return CGRequestScreenCaptureAccess() ? .granted : .denied
    }

    @discardableResult
    static func requestNotifications() async -> PermissionState {
        let center = UNUserNotificationCenter.current()
        _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
        return await notificationStatus().state
    }

    /// The prompt option is what registers EV in the Accessibility list; the
    /// return value is the trust state before the user answers.
    @discardableResult
    static func requestAccessibility() -> PermissionState {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let trusted = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
        return trusted ? .granted : .denied
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
        let string: String
        if usesSettingsExtensionScheme {
            switch kind {
            case .microphone:
                string = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone"
            case .camera:
                string = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Camera"
            case .screenRecording:
                // Renamed "Screen & System Audio Recording" in macOS 15; the
                // ScreenCapture anchor still targets it.
                string = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
            case .notifications:
                string = "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=\(bundleIdentifier)"
            case .accessibility:
                string = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
            }
        } else {
            switch kind {
            case .microphone:
                string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
            case .camera:
                string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera"
            case .screenRecording:
                string = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
            case .notifications:
                string = "x-apple.systempreferences:com.apple.preference.notifications"
            case .accessibility:
                string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
            }
        }
        return URL(string: string)
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

    private static func usageDescriptionFacts() -> [PermissionFact] {
        var required = PermissionKind.allCases.compactMap { $0.usageDescriptionKey }
        required.append("NSSpeechRecognitionUsageDescription")
        return required.map { key in
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

    private static func state(for status: AVAuthorizationStatus) -> PermissionState {
        switch status {
        case .authorized: return .granted
        case .denied: return .denied
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        @unknown default: return .notDetermined
        }
    }

    private static func microphoneStatus() -> PermissionStatus {
        PermissionStatus(
            kind: .microphone,
            state: state(for: AVCaptureDevice.authorizationStatus(for: .audio)),
            whatBreaks: "EV cannot hear the wake word \"EVIE\", and the hotkey and Talk button cannot record; wake/verify/utterance voice flows stay text-only.",
            settingsURL: settingsURL(for: .microphone)
        )
    }

    private static func cameraStatus() -> PermissionStatus {
        PermissionStatus(
            kind: .camera,
            state: state(for: AVCaptureDevice.authorizationStatus(for: .video)),
            whatBreaks: "Camera capture (share sheet / photo events) cannot run. Text and file capture still work.",
            settingsURL: settingsURL(for: .camera)
        )
    }

    private static func screenRecordingStatus() -> PermissionStatus {
        let state: PermissionState = CGPreflightScreenCaptureAccess() ? .granted : .denied
        return PermissionStatus(
            kind: .screenRecording,
            state: state,
            whatBreaks: "ScreenCaptureKit-based ambient context (Agent 13's macOS collectors) cannot see the screen.",
            settingsURL: settingsURL(for: .screenRecording)
        )
    }

    private static func notificationStatus() async -> PermissionStatus {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
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
            settingsURL: settingsURL(for: .notifications)
        )
    }

    private static func accessibilityStatus() -> PermissionStatus {
        let state: PermissionState = AXIsProcessTrusted() ? .granted : .denied
        return PermissionStatus(
            kind: .accessibility,
            state: state,
            whatBreaks: "The global hotkey cannot see key presses from other apps, so ⇧⌘E will not open the mic from anywhere.",
            settingsURL: settingsURL(for: .accessibility)
        )
    }
}

struct PermissionRowView: View {
    let status: PermissionStatus
    let onRequest: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(stateColor)
                .frame(width: 8, height: 8)
                .padding(.top, 4)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
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
                actionRow
                if let reset = status.resetCommand {
                    resetHint(command: reset)
                }
            }
            Spacer()
        }
    }

    @ViewBuilder
    private var actionRow: some View {
        HStack(spacing: 6) {
            switch status.state {
            case .notDetermined:
                Button("Request", action: onRequest)
                    .font(.caption2)
            case .denied, .restricted:
                Button("Open Settings") {
                    PermissionCenter.openSettings(for: status.kind)
                }
                .font(.caption2)
            case .granted:
                EmptyView()
            }
        }
    }

    private func resetHint(command: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("EV is already in this list with its switch off. Flip it, or re-arm the prompt with:")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Text(command)
                    .font(.system(.caption2, design: .monospaced))
                    .textSelection(.enabled)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    _ = NSPasteboard.general.setString(command, forType: .string)
                }
                .font(.caption2)
            }
        }
    }

    private var stateLabel: String {
        switch status.state {
        case .granted: return "granted"
        case .denied: return "denied"
        case .notDetermined: return "not asked"
        case .restricted: return "restricted"
        }
    }

    private var stateColor: Color {
        switch status.state {
        case .granted: return .green
        case .denied: return .red
        case .notDetermined: return .orange
        case .restricted: return .red
        }
    }
}

struct PermissionsPanelView: View {
    @State private var statuses: [PermissionStatus] = []
    @State private var facts: [PermissionFact] = []
    @State private var isRequesting = false
    @State private var showDiagnostics = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            if statuses.isEmpty {
                ProgressView()
            } else {
                ForEach(statuses, id: \.kind) { status in
                    PermissionRowView(status: status) {
                        request(status.kind)
                    }
                }
            }
            Divider()
            diagnosticsSection
        }
        .padding()
        .frame(width: 380)
        .task {
            await refresh()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Permissions")
                .font(.headline)
            Text("macOS only lists EV under Privacy & Security after EV has asked. Grant permissions asks for all of them, which is what puts EV in the list.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Grant permissions") {
                    isRequesting = true
                    Task {
                        statuses = await PermissionCenter.requestAll()
                        facts = PermissionCenter.diagnostics()
                        isRequesting = false
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(isRequesting)
                Button("Refresh") {
                    Task { await refresh() }
                }
                .font(.caption)
                if isRequesting {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
    }

    private var diagnosticsSection: some View {
        DisclosureGroup("Diagnostics", isExpanded: $showDiagnostics) {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(facts) { fact in
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(alignment: .top, spacing: 4) {
                            Text(fact.ok ? "✅" : "⚠️")
                                .font(.caption2)
                            Text("\(fact.title): \(fact.detail)")
                                .font(.caption2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let fix = fact.fix {
                            Text(fix)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.leading, 16)
                        }
                    }
                }
            }
            .padding(.top, 4)
        }
        .font(.caption)
    }

    private func request(_ kind: PermissionKind) {
        isRequesting = true
        Task {
            await PermissionCenter.request(kind)
            await refresh()
            isRequesting = false
        }
    }

    private func refresh() async {
        statuses = await PermissionCenter.statuses()
        facts = PermissionCenter.diagnostics()
    }
}
