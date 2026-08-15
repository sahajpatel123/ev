import AVFoundation
import Foundation

/// Live microphone TCC as the menu-bar UI and capture gates publish it.
///
/// Authorized is never presented as off / denied. A just-accepted consent
/// prompt is treated as granted even when `authorizationStatus` has not
/// flushed yet (menu-bar accessories often lag one read).
public enum MicrophoneGrant: String, Equatable, Sendable {
    case granted
    case denied
    case notDetermined
    case restricted

    /// Denied (or restricted) — the “off / unused-because-ungranted” states.
    public var isOffOrDenied: Bool {
        self == .denied || self == .restricted
    }

    public var isUsable: Bool {
        self == .granted
    }
}

/// Single mapping from AVFoundation / AVAudio record permission to the
/// status EV publishes. `PermissionCenter`, `MicCapture`, and live start
/// all call through here so a just-accepted prompt cannot stay stale.
public enum MicrophoneAuthorization {
    public static let didChange = Notification.Name("ev.microphoneAuthorizationDidChange")

    private static let lock = NSLock()
    private static var rememberedGrant: Bool?
    /// After Allow, TCC can still read `.denied` / `.notDetermined` for a
    /// few seconds. Elevate only inside this window so a later revoke wins.
    private static var rememberUntil: Date?
    private static var inFlight: Task<Bool, Never>?
    private static var lastPublished: MicrophoneGrant?

    /// Maps a TCC snapshot (and optional just-accepted request) to the
    /// published grant. This is the shipped permission-status function.
    public static func state(
        authorizationStatus: AVAuthorizationStatus,
        audioRecordPermissionGranted: Bool? = nil,
        requestJustGranted: Bool = false
    ) -> MicrophoneGrant {
        if authorizationStatus == .authorized {
            return .granted
        }
        if audioRecordPermissionGranted == true {
            return .granted
        }
        // Just-accepted Allow: status can still read `.notDetermined` or a
        // lagging `.denied`. Restricted is a hard OS block and stays.
        if requestJustGranted, authorizationStatus != .restricted {
            return .granted
        }
        switch authorizationStatus {
        case .authorized:
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

    /// Live read used by the Permissions row and capture start.
    public static func current() -> MicrophoneGrant {
        lock.lock()
        let remembered = rememberedGrant == true
            && (rememberUntil.map { Date() < $0 } ?? false)
        lock.unlock()
        return state(
            authorizationStatus: AVCaptureDevice.authorizationStatus(for: .audio),
            audioRecordPermissionGranted: audioAppGranted(),
            requestJustGranted: remembered
        )
    }

    /// Prompt when undecided. Serializes concurrent callers (live + panel)
    /// onto one TCC dialog. Remembers a true result so the next status
    /// read cannot publish off/denied for a just-accepted Allow.
    public static func requestAccess() async -> Bool {
        let existing = current()
        if existing == .granted {
            publishCurrent()
            return true
        }
        if existing == .restricted {
            return false
        }
        // `.denied` still re-reads below — System Settings may have flipped.

        let waiter: Task<Bool, Never>
        lock.lock()
        if let inFlight {
            waiter = inFlight
            lock.unlock()
            return await waiter.value
        }
        let task = Task<Bool, Never> {
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            remember(granted)
            // Let TCC flush so `authorizationStatus` can catch up.
            try? await Task.sleep(nanoseconds: 200_000_000)
            let presented = current()
            publishCurrent()
            return presented == .granted || granted
        }
        inFlight = task
        lock.unlock()
        let result = await task.value
        lock.lock()
        inFlight = nil
        lock.unlock()
        return result
    }

    /// Publish a change when live TCC (or a remembered Allow) flips, so
    /// the panel and live start refresh instead of staying off/denied.
    public static func publishCurrent() {
        let now = current()
        lock.lock()
        let previous = lastPublished
        lastPublished = now
        lock.unlock()
        if now != previous {
            notifyChange()
        }
    }

    public static func resetForTests() {
        lock.lock()
        rememberedGrant = nil
        rememberUntil = nil
        inFlight = nil
        lastPublished = nil
        lock.unlock()
    }

    private static func remember(_ granted: Bool) {
        lock.lock()
        if granted {
            rememberedGrant = true
            rememberUntil = Date().addingTimeInterval(8)
        } else {
            rememberedGrant = nil
            rememberUntil = nil
        }
        lock.unlock()
    }

    private static func notifyChange() {
        NotificationCenter.default.post(name: didChange, object: nil)
    }

    private static func audioAppGranted() -> Bool? {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return true
        case .denied:
            return false
        case .undetermined:
            return nil
        @unknown default:
            return nil
        }
    }
}
