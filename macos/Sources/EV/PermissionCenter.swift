import ApplicationServices
import AVFoundation
import CoreGraphics
import Foundation
import SwiftUI
import UserNotifications

enum PermissionKind: String, CaseIterable, Identifiable {
    case microphone
    case camera
    case screenRecording
    case notifications
    case accessibility

    var id: String { rawValue }
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
}

/// Detects every permission SUIT can need, explains what breaks when denied,
/// and deep-links to the exact System Settings pane. No silent failures.
enum PermissionCenter {
    static func statuses() async -> [PermissionStatus] {
        var result: [PermissionStatus] = []
        result.append(microphoneStatus())
        result.append(cameraStatus())
        result.append(screenRecordingStatus())
        result.append(await notificationStatus())
        result.append(accessibilityStatus())
        return result
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
        case .camera:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera"
        case .screenRecording:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        case .notifications:
            string = "x-apple.systempreferences:com.apple.preference.Notifications"
        case .accessibility:
            string = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        }
        return URL(string: string)
    }

    private static func microphoneStatus() -> PermissionStatus {
        let state: PermissionState
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return PermissionStatus(
            kind: .microphone,
            state: state,
            whatBreaks: "The hotkey and Talk button cannot record your voice; wake/verify/utterance voice flows stay text-only.",
            settingsURL: settingsURL(for: .microphone)
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
                Button("Open System Settings") {
                    PermissionCenter.openSettings(for: status.kind)
                }
                .font(.caption2)
            }
            Spacer()
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

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Permissions")
                .font(.headline)
            if statuses.isEmpty {
                ProgressView()
            } else {
                ForEach(statuses, id: \.kind) { status in
                    PermissionRowView(status: status)
                }
            }
        }
        .padding()
        .frame(width: 380)
        .task {
            statuses = await PermissionCenter.statuses()
        }
    }
}
