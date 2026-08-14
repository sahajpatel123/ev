import AVFoundation
import Contacts
import CoreBluetooth
import CoreLocation
import EVClient
import Speech
import SwiftUI
import UserNotifications

/// iOS-side "Grant EVIE access" screen and status collector for both phones.
enum iOSPermissionKind: String, CaseIterable, Identifiable {
    case contacts
    case microphone
    case speech
    case camera
    case notifications
    case location
    case bluetooth

    var id: String { rawValue }
}

enum iOSPermissionState: String {
    case granted
    case denied
    case notDetermined
    case restricted
}

struct iOSPermissionStatus: Identifiable {
    let kind: iOSPermissionKind
    let state: iOSPermissionState
    let whatBreaks: String
    let canRequest: Bool

    var id: String { kind.rawValue }
}

enum IOSPermissionCenter {
    static func statuses() async -> [iOSPermissionStatus] {
        [
            contactsStatus(),
            microphoneStatus(),
            speechStatus(),
            cameraStatus(),
            await notificationStatus(),
            locationStatus(),
            bluetoothStatus(),
        ]
    }

    static func request(_ kind: iOSPermissionKind) async -> Bool {
        switch kind {
        case .contacts:
            return (try? await CNContactStore().requestAccess(for: .contacts)) ?? false
        case .microphone:
            return await AVCaptureDevice.requestAccess(for: .audio)
        case .speech:
            return await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { _ in
                    continuation.resume(returning: true)
                }
            }
        case .camera:
            return await AVCaptureDevice.requestAccess(for: .video)
        case .notifications:
            return (try? await UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .sound, .badge]
            )) ?? false
        case .location:
            CLLocationManager().requestWhenInUseAuthorization()
            return true
        case .bluetooth:
            if let url = URL(string: UIApplication.openSettingsURLString) {
                await UIApplication.shared.open(url)
            }
            return false
        }
    }

    private static func contactsStatus() -> iOSPermissionStatus {
        let state: iOSPermissionState
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .contacts,
            state: state,
            whatBreaks: "EV cannot resolve contacts or address texts/calls by name.",
            canRequest: true
        )
    }

    private static func microphoneStatus() -> iOSPermissionStatus {
        let state: iOSPermissionState
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .microphone,
            state: state,
            whatBreaks: "Voice capture (wake, verify, utterance) cannot record audio.",
            canRequest: true
        )
    }

    private static func speechStatus() -> iOSPermissionStatus {
        let state: iOSPermissionState
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .speech,
            state: state,
            whatBreaks: "On-device dictation for quick capture cannot run.",
            canRequest: true
        )
    }

    private static func cameraStatus() -> iOSPermissionStatus {
        let state: iOSPermissionState
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .camera,
            state: state,
            whatBreaks: "Photo/video capture notes cannot use the camera.",
            canRequest: true
        )
    }

    private static func notificationStatus() async -> iOSPermissionStatus {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        let state: iOSPermissionState
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .notifications,
            state: state,
            whatBreaks: "Alerts/digests cannot reach the Lock Screen.",
            canRequest: true
        )
    }

    private static func locationStatus() -> iOSPermissionStatus {
        let status = CLLocationManager().authorizationStatus
        let state: iOSPermissionState
        switch status {
        case .authorizedAlways, .authorizedWhenInUse: state = .granted
        case .denied: state = .denied
        case .notDetermined: state = .notDetermined
        case .restricted: state = .restricted
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .location,
            state: state,
            whatBreaks: "Location-aware HUD cards cannot use your position.",
            canRequest: true
        )
    }

    private static func bluetoothStatus() -> iOSPermissionStatus {
        let state: iOSPermissionState
        switch CBManager.authorization {
        case .allowedAlways: state = .granted
        case .denied, .restricted: state = .denied
        case .notDetermined: state = .notDetermined
        @unknown default: state = .notDetermined
        }
        return iOSPermissionStatus(
            kind: .bluetooth,
            state: state,
            whatBreaks: "Bluetooth peripherals (health belt, accessories) cannot be used.",
            canRequest: false
        )
    }
}

struct GrantAccessView: View {
    @State private var statuses: [iOSPermissionStatus] = []

    var body: some View {
        NavigationStack {
            List(statuses) { status in
                HStack(alignment: .top) {
                    Circle()
                        .fill(stateColor(status.state))
                        .frame(width: 8, height: 8)
                        .padding(.top, 6)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(status.kind.rawValue)
                            .font(.subheadline)
                            .fontWeight(.semibold)
                        Text(status.whatBreaks)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if status.state != .granted {
                        Button(status.canRequest ? "Request" : "Settings") {
                            Task {
                                _ = await IOSPermissionCenter.request(status.kind)
                                await refresh()
                            }
                        }
                        .font(.caption)
                    } else {
                        Text("granted")
                            .font(.caption)
                            .foregroundStyle(.green)
                    }
                }
            }
            .navigationTitle("Grant EVIE access")
            .toolbar {
                Button("Refresh") {
                    Task { await refresh() }
                }
            }
            .task { await refresh() }
        }
    }

    private func refresh() async {
        statuses = await IOSPermissionCenter.statuses()
    }

    private func stateColor(_ state: iOSPermissionState) -> Color {
        switch state {
        case .granted: return .green
        case .denied, .restricted: return .red
        case .notDetermined: return .orange
        }
    }
}

/// Builds the backend permission report from live iOS statuses.
enum EVLifeSync {
    static func report(client: EVAPIClient, deviceID: String) async -> Bool {
        let statuses = await IOSPermissionCenter.statuses()
        let entries = statuses.map {
            EVPermissionEntry(permission: $0.kind.rawValue, state: $0.state.rawValue)
        }
        let report = EVPermissionReport(
            platform: "ios",
            deviceId: deviceID,
            permissions: entries
        )
        do {
            return try await client.postPermissionReport(report, deviceID: deviceID)
        } catch {
            return false
        }
    }
}
