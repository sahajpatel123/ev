import AppKit
import CoreGraphics
import Foundation

/// File-scope so the pure (nonisolated) label rule can read it without
/// touching main-actor state.
private let kSensitiveBundleIds: Set<String> = [
    "com.agilebits.onepassword7",
    "com.1password.1password",
    "com.apple.keychainaccess",
]

/// App-local producer of `ambient.looking_at`: which app/window is frontmost.
///
/// - Needs no new permission (`NSWorkspace` + window titles; only image
///   capture requires Screen Recording).
/// - Window titles never leave the Mac: this event is consumed by the local
///   store only, never sent to the backend.
/// - Sensitive apps (owner exclusion list + built-ins) yield an empty label:
///   the HUD shows nothing rather than a redaction marker.
@MainActor
public final class LookingAtMonitor {
    public var onChange: ((LookingAt) -> Void)?

    private var observer: NSObjectProtocol?
    private var timer: Timer?
    private var last = LookingAt()

    public init() {}

    public func start() {
        stop()
        observer = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        // Window titles change without app switches (tab changes, new mail):
        // re-read on a slow poll. Passive observation, no events consumed.
        let timer = Timer(timeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        refresh()
    }

    public func stop() {
        if let observer {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
        observer = nil
        timer?.invalidate()
        timer = nil
    }

    private func refresh() {
        let next = Self.current()
        if next != last {
            last = next
            onChange?(next)
        }
    }

    // MARK: - Pure rules (checked headless)

    /// Bundle ids whose window titles must never appear, even locally.
    public static nonisolated var sensitiveBundleIds: Set<String> { kSensitiveBundleIds }

    public static nonisolated func userExcludedBundleIds(
        _ defaults: UserDefaults = .standard
    ) -> Set<String> {
        Set(defaults.stringArray(forKey: "EV_SENSITIVE_BUNDLE_IDS") ?? [])
    }

    /// Blank the label for sensitive apps. Pure: the checks drive this.
    public static nonisolated func label(
        app: String, window: String, bundleId: String,
        userExcluded: Set<String> = []
    ) -> LookingAt {
        if sensitiveBundleIds.contains(bundleId) || userExcluded.contains(bundleId) {
            return LookingAt()
        }
        return LookingAt(app: app, window: window, bundleId: bundleId)
    }

    // MARK: - Live read (never called from checks)

    static func current() -> LookingAt {
        guard let front = NSWorkspace.shared.frontmostApplication else {
            return LookingAt()
        }
        let app = front.localizedName ?? ""
        let bundleId = front.bundleIdentifier ?? ""
        let window = frontWindowTitle(pid: front.processIdentifier)
        return label(
            app: app, window: window, bundleId: bundleId,
            userExcluded: userExcludedBundleIds()
        )
    }

    private static func frontWindowTitle(pid: pid_t) -> String {
        guard let list = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
        ) as? [[String: Any]] else {
            return ""
        }
        for entry in list {
            guard (entry[kCGWindowOwnerPID as String] as? Int32) == pid,
                  (entry[kCGWindowLayer as String] as? Int) == 0,
                  let name = entry[kCGWindowName as String] as? String,
                  !name.isEmpty
            else {
                continue
            }
            return name
        }
        return ""
    }
}
