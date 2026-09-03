import AppKit
import ApplicationServices
import Carbon.HIToolbox
import CoreGraphics
import Darwin
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

/// Authoritative native Mac interaction boundary for EV.
///
/// App lifecycle, Accessibility UI, keyboard, windows, and on-demand
/// screenshots all run here. Invoked from the live websocket, never from
/// the audio render thread.
public final class MacControlService: @unchecked Sendable {
    public static let shared = MacControlService()

    /// Serializes handle() without a GCD queue. AppleScript hops to the
    /// main run loop; a GCD ``queue.sync`` from that path deadlocks Talk.
    private let workLock = NSLock()
    private var generation = 0
    private var snapshotID = ""
    private var elements: [String: ElementRecord] = [:]
    private var frames: [String: FrameRecord] = [:]
    private var cancelled = Set<String>()
    private var lastBundle: String?
    private var lastApp: String?
    private var lastPlaylist: String?
    private var lastTrackIndex: Int?
    private var lastNoteName: String?
    private var lastNoteBody: String?
    private var lastSafariQuery: String?
    private var lastChromeQuery: String?
    private var focusedNow: AXUIElement?
    private var installedAppsCache: (stamp: String, apps: [[String: Any]])?
    private var appDirSources: [DispatchSourceFileSystemObject] = []

    private struct ElementRecord {
        let ref: String
        let element: AXUIElement
        let generation: Int
        let pid: pid_t
        let bundleId: String
        let role: String
        let title: String
        let secure: Bool
    }

    private struct FrameRecord {
        let frameId: String
        let bundleId: String
        let windowID: CGWindowID
        let bounds: CGRect
        let capturedAt: Date
    }

    private final class NativeDataBox: @unchecked Sendable {
        var data: Data?
    }

    private init() {
        startAppCatalogWatch()
    }

    public func permissionSnapshot() -> [String: Any] {
        let front = NSWorkspace.shared.frontmostApplication
        let trusted = AXIsProcessTrusted()
        let probe = trusted ? functionalAccessibilityProbe() : [:]
        let probeOk = probe["ok"] as? Bool == true
        var payload: [String: Any] = [
            "accessibility_permission": trusted ? "authorized" : "denied",
            "screen_capture_permission": CGPreflightScreenCaptureAccess() ? "authorized" : "denied",
            "apple_events_permission": "unknown",
            "foreground_app": front?.localizedName as Any,
            "foreground_bundle_id": front?.bundleIdentifier as Any,
            "platform": "macos",
            "accessibility_ready": trusted && probeOk,
            "generic_ui_control_ready": trusted && probeOk,
            "app_lifecycle_ready": true,
            "screen_vision_ready": CGPreflightScreenCaptureAccess(),
            "ax_process": processIdentity(),
            "settings_url": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ]
        if !probe.isEmpty { payload["accessibility_probe"] = probe }
        if trusted && !probeOk {
            payload["reason"] = "ax_probe_failed"
            payload["relaunch_required"] = true
        }
        return payload
    }

    /// Cheap permission bits for the live computer-state watch. The full
    /// ``permissionSnapshot`` walks up to 80 AX nodes (0.6s deadline) and
    /// must not run on a timer — that heated the Mac while idle listening.
    public func permissionFlags() -> [String: Any] {
        let front = NSWorkspace.shared.frontmostApplication
        let trusted = AXIsProcessTrusted()
        return [
            "accessibility_permission": trusted ? "authorized" : "denied",
            "screen_capture_permission": CGPreflightScreenCaptureAccess() ? "authorized" : "denied",
            "apple_events_permission": "unknown",
            "foreground_app": front?.localizedName as Any,
            "foreground_bundle_id": front?.bundleIdentifier as Any,
            "platform": "macos",
            "accessibility_ready": trusted,
            "generic_ui_control_ready": trusted,
            "app_lifecycle_ready": true,
            "screen_vision_ready": CGPreflightScreenCaptureAccess(),
            "installed_app_stamp": applicationsStamp(),
            "installed_app_count": installedApps().count,
        ]
    }

    public func handle(command: String, arguments: [String: Any], requestId: String) -> [String: Any] {
        // NSLock, not GCD queue.sync: Chrome AppleScript hops to the main
        // run loop. Holding `queue` while waiting for main deadlocks Talk
        // (permission/state work and Swift MainActor share that thread).
        workLock.lock()
        defer { workLock.unlock() }
        if cancelled.contains(requestId) {
            return fail("cancelled", "Stopped.", command: command, requestId: requestId)
        }
        switch command {
        case "status":
            return status(requestId: requestId)
        case "list_apps":
            return listApps(arguments, requestId: requestId)
        case "open_app":
            return openApp(arguments, requestId: requestId, activate: true)
        case "activate_app":
            return openApp(arguments, requestId: requestId, activate: true)
        case "close_app":
            return closeApp(arguments, requestId: requestId)
        case "open_url":
            return openURL(arguments, requestId: requestId)
        case "inspect_ui":
            return inspectUI(arguments, requestId: requestId)
        case "ui_action":
            return uiAction(arguments, requestId: requestId)
        case "screen_look":
            return screenLook(arguments, requestId: requestId)
        case "app_action":
            return appAction(arguments, requestId: requestId)
        case "keyboard":
            return keyboard(arguments, requestId: requestId)
        case "window_op":
            return windowOp(arguments, requestId: requestId)
        case "cancel":
            cancelled.insert(requestId)
            if let other = arguments["request_id"] as? String { cancelled.insert(other) }
            return ok(["cancelled": true, "spoken": "Stopped."], command: command, requestId: requestId)
        case "request_accessibility":
            return requestAccessibility(requestId: requestId)
        case "file_op":
            return fileOp(arguments, requestId: requestId)
        default:
            return fail("unknown_command", "Unknown Mac control command.", command: command, requestId: requestId)
        }
    }

    public func cancel(requestId: String) {
        workLock.lock()
        cancelled.insert(requestId)
        workLock.unlock()
    }

    // MARK: - Status / apps

    private func status(requestId: String) -> [String: Any] {
        var payload = permissionSnapshot()
        payload["ok"] = true
        payload["command"] = "status"
        payload["request_id"] = requestId
        payload["bluetooth_on"] = bluetoothPowered() as Any
        let uiReady = payload["generic_ui_control_ready"] as? Bool == true
        let trusted = AXIsProcessTrusted()
        if uiReady {
            payload["spoken"] = "I can operate apps on this Mac."
        } else if trusted {
            payload["spoken"] = "macOS granted Accessibility, but EV still cannot read UI. Quit and reopen EV, then try again."
        } else {
            payload["spoken"] = "I can open and close apps, but macOS hasn't given me Accessibility access yet, so I can't operate their controls."
        }
        return payload
    }

    private func listApps(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let query = (string(arguments, "query") ?? string(arguments, "name") ?? "").lowercased()
        let runningOnly = bool(arguments, "running_only") || bool(arguments, "running")
        var apps = runningApps()
        if !runningOnly {
            apps.append(contentsOf: installedApps())
        }
        var seen = Set<String>()
        var unique: [[String: Any]] = []
        for app in apps {
            let bundle = (app["bundle_id"] as? String)?.lowercased() ?? ""
            if bundle.isEmpty || seen.contains(bundle) { continue }
            if !query.isEmpty {
                let name = (app["name"] as? String ?? "").lowercased()
                if !name.contains(query) && !bundle.contains(query) { continue }
            }
            seen.insert(bundle)
            unique.append(app)
            if unique.count >= 200 { break }
        }
        let names = unique.compactMap { $0["name"] as? String }
        return ok(
            [
                "apps": unique,
                "count": unique.count,
                "catalog_stamp": applicationsStamp(),
                "installed_app_names": names,
                "spoken": unique.isEmpty ? "I didn't find a matching app." : "I found \(unique.count) apps.",
            ],
            command: "list_apps",
            requestId: requestId
        )
    }

    private func openApp(_ arguments: [String: Any], requestId: String, activate: Bool) -> [String: Any] {
        let rawName = (string(arguments, "name") ?? string(arguments, "app") ?? "").lowercased()
        if rawName == "downloads" || rawName == "download" || rawName == "downloads folder" {
            return openDownloads(requestId: requestId)
        }
        if let pane = Self.settingsPaneURL(for: rawName) {
            return openSettingsPane(pane, requestId: requestId)
        }
        guard let resolved = resolve(name: string(arguments, "name") ?? string(arguments, "app"), bundleId: string(arguments, "bundle_id")) else {
            return fail("not_found", "I couldn't find that app.", command: "open_app", requestId: requestId)
        }
        if Self.protected.contains(resolved.bundle.lowercased()) && string(arguments, "action") == "quit" {
            return fail("protected", "I won't quit \(resolved.name).", command: "open_app", requestId: requestId)
        }
        var launched = false
        if let running = resolved.running {
            running.unhide()
            running.activate(options: [.activateAllWindows])
        } else if let url = resolved.url {
            launched = NSWorkspace.shared.open(url)
            Thread.sleep(forTimeInterval: 0.35)
            NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle).first?
                .activate(options: [.activateAllWindows])
        } else {
            return fail("not_found", "I couldn't find \(resolved.name).", command: "open_app", requestId: requestId)
        }
        let nowRunning = NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle).first
        let front = nowRunning?.isActive == true || NSWorkspace.shared.frontmostApplication?.bundleIdentifier == resolved.bundle
        lastBundle = resolved.bundle
        lastApp = resolved.name
        return ok(
            [
                "name": resolved.name,
                "app": resolved.name,
                "bundle_id": resolved.bundle,
                "opened": nowRunning != nil,
                "activated": front || activate,
                "launched": launched,
                "running": nowRunning != nil,
                "pid": nowRunning?.processIdentifier as Any,
                "executed": nowRunning != nil,
                "verified": nowRunning != nil,
                "spoken": nowRunning != nil ? "Opened \(resolved.name)." : "I couldn't open \(resolved.name).",
                "verification_hint": nowRunning != nil ? "\(resolved.name) is running" : "",
                "control": Self.adapterControl(app: resolved.name, bundleId: resolved.bundle),
                "goal_complete": false,
            ],
            command: "open_app",
            requestId: requestId,
            ok: nowRunning != nil
        )
    }

    private func dismissAppWindows(_ resolved: ResolvedApp, requestId: String) -> [String: Any] {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle)
        guard !running.isEmpty else {
            return ok(
                [
                    "name": resolved.name,
                    "app": resolved.name,
                    "bundle_id": resolved.bundle,
                    "closed": true,
                    "closed_windows": true,
                    "already_closed": true,
                    "quit": false,
                    "spoken": "\(resolved.name) windows weren't open.",
                ],
                command: "close_app",
                requestId: requestId
            )
        }
        let script = """
        tell application id \(asLiteral(resolved.bundle))
          close every window
        end tell
        """
        let ran = runAppleScript(script)
        running.first?.hide()
        Thread.sleep(forTimeInterval: 0.25)
        let check = runAppleScript("""
        tell application id \(asLiteral(resolved.bundle))
          return (count of windows) as string
        end tell
        """)
        let leftover = Int(check.text.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0
        let closed = leftover == 0
        return ok(
            [
                "name": resolved.name,
                "app": resolved.name,
                "bundle_id": resolved.bundle,
                "closed": closed,
                "closed_windows": true,
                "quit": false,
                "spoken": closed
                    ? "Closed \(resolved.name) windows."
                    : (ran.ok ? "Hid \(resolved.name)." : "I couldn't close \(resolved.name) windows."),
            ],
            command: "close_app",
            requestId: requestId,
            ok: closed || ran.ok
        )
    }

    private func closeApp(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard let resolved = resolve(name: string(arguments, "name") ?? string(arguments, "app"), bundleId: string(arguments, "bundle_id")) else {
            return fail("not_found", "I couldn't find that app.", command: "close_app", requestId: requestId)
        }
        if resolved.bundle.lowercased() == "com.apple.loginwindow" {
            return fail("protected", "I won't quit \(resolved.name).", command: "close_app", requestId: requestId)
        }
        if Self.protected.contains(resolved.bundle.lowercased()) {
            return dismissAppWindows(resolved, requestId: requestId)
        }
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle)
        guard let app = running.first else {
            return ok(
                [
                    "name": resolved.name,
                    "app": resolved.name,
                    "bundle_id": resolved.bundle,
                    "closed": true,
                    "already_closed": true,
                    "quit": true,
                    "spoken": "\(resolved.name) wasn't open.",
                ],
                command: "close_app",
                requestId: requestId
            )
        }
        let force = bool(arguments, "force")
        let requested = force ? app.forceTerminate() : app.terminate()
        let deadline = Date().addingTimeInterval(2.5)
        while Date() < deadline {
            if NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle).isEmpty {
                break
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        let still = !NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle).isEmpty
        if still && !force {
            return ok(
                [
                    "name": resolved.name,
                    "app": resolved.name,
                    "bundle_id": resolved.bundle,
                    "closed": false,
                    "quit": requested,
                    "dialog_present": true,
                    "spoken": "\(resolved.name) is still open — it may be asking to save.",
                    "verification_hint": "inspect the save dialog",
                ],
                command: "close_app",
                requestId: requestId,
                ok: false
            )
        }
        return ok(
            [
                "name": resolved.name,
                "app": resolved.name,
                "bundle_id": resolved.bundle,
                "closed": !still,
                "quit": !still,
                "already_closed": false,
                "spoken": still ? "I couldn't close \(resolved.name)." : "Closed \(resolved.name).",
            ],
            command: "close_app",
            requestId: requestId,
            ok: !still
        )
    }

    private static let allowedURLSchemes: Set<String> = [
        "http", "https", "mailto", "maps", "message", "sms", "tel",
        "facetime", "spotify", "notes", "music", "itms", "itmss",
    ]

    private func openURL(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard let raw = string(arguments, "url"), let url = URL(string: raw),
              let scheme = url.scheme?.lowercased(),
              Self.allowedURLSchemes.contains(scheme)
        else {
            return fail("invalid_url", "I can only open web links and a few app links.", command: "open_url", requestId: requestId)
        }
        let opened = NSWorkspace.shared.open(url)
        return ok(
            ["url": raw, "opened": opened, "spoken": opened ? "Opened \(raw)." : "I couldn't open that link."],
            command: "open_url",
            requestId: requestId,
            ok: opened
        )
    }

    // MARK: - Accessibility

    private func inspectUI(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard ensureAccessibility() else {
            return accessibilityDenied(command: "inspect_ui", requestId: requestId)
        }
        let target = resolve(
            name: string(arguments, "app") ?? string(arguments, "name"),
            bundleId: string(arguments, "bundle_id")
        )
        var running = target?.running ?? NSWorkspace.shared.frontmostApplication
        if let bundle = running?.bundleIdentifier {
            let appEl = AXUIElementCreateApplication(running!.processIdentifier)
            if axElements(appEl, kAXWindowsAttribute).isEmpty, let better = pickUIProcess(bundleId: bundle) {
                running = better
            }
        }
        guard let running else {
            return fail("not_running", "No application is in front.", command: "inspect_ui", requestId: requestId)
        }
        if target != nil, running.bundleIdentifier != target?.bundle {
            let shouldActivate = arguments["activate"] == nil || bool(arguments, "activate")
            if shouldActivate {
                running.unhide()
                running.activate(options: [.activateAllWindows])
                Thread.sleep(forTimeInterval: 0.25)
            }
        }
        let pid = running.processIdentifier
        let appEl = AXUIElementCreateApplication(pid)
        generation += 1
        snapshotID = "s\(generation)"
        elements.removeAll(keepingCapacity: true)
        let query = (string(arguments, "query") ?? "").lowercased()
        let level = (string(arguments, "level") ?? (query.isEmpty ? "summary" : "targeted")).lowercased()
        let defaultMax = level == "summary" ? 12 : (level == "expanded" ? 70 : (query.isEmpty ? 40 : 28))
        let maxElements = min(max(int(arguments, "max_elements") ?? defaultMax, 8), 90)
        let focusedWindow = axElement(appEl, kAXFocusedWindowAttribute)
        let windows = axElements(appEl, kAXWindowsAttribute)
        let window = focusedWindow ?? windows.first
        let windowTitle = window.flatMap { stringValue($0, kAXTitleAttribute) } ?? ""
        let sheets = windows.filter { isDialog($0) }
        let dialog = isDialog(window) || !sheets.isEmpty
        focusedNow = axElement(appEl, kAXFocusedUIElementAttribute)
        var walked: [WalkItem] = []
        var walkCount = 0
        let deadline = Date().addingTimeInterval(query.isEmpty ? 1.8 : 2.2)
        let maxWalk = query.isEmpty ? 280 : 420
        for sheet in sheets {
            walk(sheet, depth: 0, deadline: deadline, maxWalk: maxWalk, query: query, into: &walked, walkedCount: &walkCount)
        }
        if let window, !containsElement(sheets, window) {
            walk(window, depth: 0, deadline: deadline, maxWalk: maxWalk, query: query, into: &walked, walkedCount: &walkCount)
        }
        if walked.count < 12 {
            for extra in windows {
                if containsElement(sheets, extra) { continue }
                if let window, CFEqual(extra, window) { continue }
                walk(extra, depth: 0, deadline: deadline, maxWalk: maxWalk, query: query, into: &walked, walkedCount: &walkCount)
                if walkCount >= maxWalk { break }
            }
        }
        let selected = selectWalkItems(walked, query: query, maxElements: maxElements)
        if !query.isEmpty && selected.isEmpty {
            let scrolled = scrollWalked(walked)
            if scrolled {
                Thread.sleep(forTimeInterval: 0.18)
                walked.removeAll(keepingCapacity: true)
                walkCount = 0
                if let window {
                    walk(window, depth: 0, deadline: Date().addingTimeInterval(1.2), maxWalk: maxWalk, query: query, into: &walked, walkedCount: &walkCount)
                }
            }
        }
        let finalSelected = selectWalkItems(walked, query: query, maxElements: maxElements)
        var collected: [[String: Any]] = []
        var compact: [String] = []
        for item in finalSelected {
            emit(item, pid: pid, bundleId: running.bundleIdentifier ?? "", into: &collected, compact: &compact)
        }
        lastBundle = running.bundleIdentifier
        lastApp = running.localizedName
        let spoken: String
        if dialog {
            spoken = "\(running.localizedName ?? "The app") is showing a dialog."
        } else if !query.isEmpty && collected.isEmpty {
            spoken = "I didn't find “\(query)” in \(running.localizedName ?? "the app")."
        } else {
            spoken = "I'm looking at \(running.localizedName ?? "the app")."
        }
        var payload: [String: Any] = [
                "snapshot_id": snapshotID,
                "generation": generation,
                "app": running.localizedName as Any,
                "active_app": running.localizedName as Any,
                "bundle_id": running.bundleIdentifier as Any,
                "pid": pid,
                "surface_pid": pid,
                "surface_name": running.localizedName as Any,
                "activation_policy": running.activationPolicy == .regular ? "regular" : "accessory",
                "window": windowTitle,
                "window_count": windows.count,
                "dialog_present": dialog,
                "query": query,
                "compact": compact.joined(separator: "\n"),
                "elements": collected,
                "walked": walked.count,
                "spoken": spoken,
        ]
        if !query.isEmpty && collected.isEmpty {
            payload["next_hint"] = "Target not in this snapshot. Try app_action for Music, scroll, or screen_look."
            payload["target_found"] = false
        } else if !query.isEmpty {
            payload["target_found"] = !collected.isEmpty
        }
        payload["searched_scope"] = windowTitle.isEmpty ? (running.localizedName ?? "front_window") : windowTitle
        payload["scrollable_regions"] = Array(walked.filter {
            $0.role.contains("Scroll") || $0.role.contains("Table") || $0.role.contains("List")
        }.prefix(6).map { $0.title.isEmpty ? $0.role : $0.title })
        let bundle = running.bundleIdentifier ?? ""
        payload["semantic_adapter_available"] = Self.adapterControl(app: running.localizedName, bundleId: bundle)["semantic_adapter"] != nil
        payload["screen_fallback_available"] = CGPreflightScreenCaptureAccess()
        return ok(
            payload,
            command: "inspect_ui",
            requestId: requestId
        )
    }

    private func uiAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "press").lowercased()
        if action == "click_at" || action == "screen_click" {
            return clickAt(arguments, requestId: requestId)
        }
        guard ensureAccessibility() else {
            return accessibilityDenied(command: "ui_action", requestId: requestId)
        }
        if action == "keyboard" || arguments["keys"] != nil && arguments["element_ref"] == nil {
            return keyboard(arguments, requestId: requestId)
        }
        if action == "scroll" && string(arguments, "element_ref") == nil {
            return scrollFront(arguments, requestId: requestId)
        }
        if (action == "paste" || action == "clipboard_paste")
            && (string(arguments, "text") ?? string(arguments, "value") ?? "").isEmpty
            && string(arguments, "element_ref") == nil
        {
            let posted = postHotkey("cmd+v")
            return ok(
                ["action": "paste", "method": "clipboard", "spoken": posted ? "Pasted." : "I couldn't paste."],
                command: "ui_action",
                requestId: requestId,
                ok: posted
            )
        }
        guard let ref = string(arguments, "element_ref") else {
            if action == "type" || action == "type_text" || action == "append" || action == "paste" {
                var value = string(arguments, "value") ?? string(arguments, "text") ?? ""
                if action == "paste", value.isEmpty {
                    let posted = postHotkey("cmd+v")
                    return ok(
                        ["action": "paste", "method": "clipboard", "spoken": posted ? "Pasted." : "I couldn't paste."],
                        command: "ui_action",
                        requestId: requestId,
                        ok: posted
                    )
                }
                if action == "append", !value.isEmpty, !value.hasPrefix("\n") {
                    value = "\n" + value
                }
                let mode = action == "append" ? "append" : "insert"
                return enterText(value, mode: mode, element: nil, requestId: requestId)
            }
            return fail("missing_element", "I need a UI element to act on.", command: "ui_action", requestId: requestId)
        }
        guard let record = elements[ref], record.generation == generation else {
            return fail("stale_element", "That UI target is stale. I'll inspect the window again.", command: "ui_action", requestId: requestId)
        }
        if record.secure && (action == "set_value" || action == "type" || action == "type_text" || action == "append" || action == "replace") {
            return fail("sensitive_field", "I won't type into a password field.", command: "ui_action", requestId: requestId)
        }
        let element = record.element
        var changed = false
        switch action {
        case "press", "click":
            changed = AXUIElementPerformAction(element, kAXPressAction as CFString) == .success
            if !changed, let point = hidCenter(of: element) {
                click(point)
                changed = true
            }
        case "double_click":
            if let point = hidCenter(of: element) {
                doubleClick(point)
                changed = true
            } else {
                changed = AXUIElementPerformAction(element, kAXPressAction as CFString) == .success
                    && AXUIElementPerformAction(element, kAXPressAction as CFString) == .success
            }
        case "right_click":
            if let point = hidCenter(of: element) {
                rightClick(point)
                changed = true
            } else {
                changed = AXUIElementPerformAction(element, "AXExpand" as CFString) == .success
                    || AXUIElementPerformAction(element, kAXShowMenuAction as CFString) == .success
            }
        case "drag":
            return drag(arguments, from: record, requestId: requestId)
        case "focus":
            changed = AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success
        case "set_value", "replace":
            let value = string(arguments, "value") ?? string(arguments, "text") ?? ""
            return enterText(value, mode: "replace", element: element, requestId: requestId)
        case "append":
            let addition = string(arguments, "value") ?? string(arguments, "text") ?? ""
            return enterText(addition, mode: "append", element: element, requestId: requestId)
        case "type", "type_text", "paste":
            let value = string(arguments, "value") ?? string(arguments, "text") ?? ""
            if action == "paste", value.isEmpty {
                _ = AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
                Thread.sleep(forTimeInterval: 0.05)
                let posted = postHotkey("cmd+v")
                return ok(
                    [
                        "action": "paste",
                        "method": "clipboard",
                        "element_ref": ref,
                        "spoken": posted ? "Pasted." : "I couldn't paste.",
                    ],
                    command: "ui_action",
                    requestId: requestId,
                    ok: posted
                )
            }
            return enterText(value, mode: "insert", element: element, requestId: requestId)
        case "select":
            changed = AXUIElementSetAttributeValue(element, kAXSelectedAttribute as CFString, kCFBooleanTrue) == .success
            if !changed {
                changed = AXUIElementPerformAction(element, kAXPressAction as CFString) == .success
            }
        case "increment":
            changed = AXUIElementPerformAction(element, kAXIncrementAction as CFString) == .success
        case "decrement":
            changed = AXUIElementPerformAction(element, kAXDecrementAction as CFString) == .success
        case "expand", "menu":
            changed = AXUIElementPerformAction(element, "AXExpand" as CFString) == .success
                || AXUIElementPerformAction(element, kAXShowMenuAction as CFString) == .success
            if !changed, let point = hidCenter(of: element) {
                rightClick(point)
                changed = true
            }
        case "collapse":
            changed = AXUIElementPerformAction(element, "AXCollapse" as CFString) == .success
        case "confirm":
            changed = AXUIElementPerformAction(element, kAXConfirmAction as CFString) == .success
        case "cancel":
            changed = AXUIElementPerformAction(element, kAXCancelAction as CFString) == .success
        case "raise":
            changed = AXUIElementPerformAction(element, kAXRaiseAction as CFString) == .success
        case "scroll":
            changed = scroll(element, arguments)
        default:
            changed = AXUIElementPerformAction(element, action as CFString) == .success
        }
        Thread.sleep(forTimeInterval: 0.12)
        let post = inspectUI(["max_elements": 40], requestId: requestId)
        var payload: [String: Any] = [
            "action": action,
            "target": record.title.isEmpty ? ref : record.title,
            "element_ref": ref,
            "ui_changed": changed,
            "app": post["app"] as Any,
            "active_app": post["app"] as Any,
            "bundle_id": post["bundle_id"] as Any,
            "window": post["window"] as Any,
            "dialog_present": post["dialog_present"] as Any,
            "new_focus": focusedTitle(pid: record.pid),
            "compact": post["compact"] as Any,
            "snapshot_id": post["snapshot_id"] as Any,
            "generation": post["generation"] as Any,
            "elements": post["elements"] as Any,
            "spoken": changed ? "Done." : "That didn't take.",
            "verification_hint": post["window"] as Any,
        ]
        if let value = post["ok"] as? Bool, !value {
            payload["inspect_error"] = post["error"] as Any
        }
        return ok(payload, command: "ui_action", requestId: requestId, ok: changed)
    }

    private func clickAt(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard let frameId = string(arguments, "frame_id"), let frame = frames[frameId] else {
            return fail("stale_frame", "That screenshot is gone. I'll look at the window again.", command: "ui_action", requestId: requestId)
        }
        if Date().timeIntervalSince(frame.capturedAt) > 8 {
            return fail("stale_frame", "That screenshot is too old to click.", command: "ui_action", requestId: requestId)
        }
        if !frame.bundleId.isEmpty {
            let selfBundle = Bundle.main.bundleIdentifier
            let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
            if front != frame.bundleId {
                _ = activateBundle(frame.bundleId)
            }
            let after = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
            let target = NSRunningApplication.runningApplications(withBundleIdentifier: frame.bundleId).first
            let targetReady = target?.isActive == true || after == frame.bundleId || after == selfBundle
            if !targetReady {
                var payload = fail(
                    "app_changed",
                    "The front app changed since that screenshot. I did not click.",
                    command: "ui_action",
                    requestId: requestId
                )
                payload["front_app"] = after as Any
                payload["expected_bundle"] = frame.bundleId
                payload["method"] = "coordinate"
                return payload
            }
        }
        let xNorm = double(arguments, "x_normalized") ?? double(arguments, "x") ?? -1
        let yNorm = double(arguments, "y_normalized") ?? double(arguments, "y") ?? -1
        guard xNorm >= 0, xNorm <= 1, yNorm >= 0, yNorm <= 1 else {
            return fail("bad_coordinates", "Click coordinates must be normalized 0–1.", command: "ui_action", requestId: requestId)
        }
        let point = CGPoint(
            x: frame.bounds.minX + frame.bounds.width * xNorm,
            y: frame.bounds.minY + frame.bounds.height * yNorm
        )
        let moved = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)
        moved?.post(tap: .cghidEventTap)
        click(point)
        Thread.sleep(forTimeInterval: 0.15)
        var payload: [String: Any] = [
            "action": "click_at",
            "frame_id": frameId,
            "x_normalized": xNorm,
            "y_normalized": yNorm,
            "ui_changed": true,
            "method": "coordinate",
            "spoken": "Clicked.",
        ]
        if AXIsProcessTrusted() {
            let post = inspectUI([:], requestId: requestId)
            payload["app"] = post["app"] as Any
            payload["dialog_present"] = post["dialog_present"] as Any
            payload["compact"] = post["compact"] as Any
            payload["snapshot_id"] = post["snapshot_id"] as Any
        }
        if CGPreflightScreenCaptureAccess() {
            let look = screenLook(["target": "active_window"], requestId: requestId)
            payload["verify_frame_id"] = look["frame_id"] as Any
            payload["width"] = look["width"] as Any
            payload["height"] = look["height"] as Any
            payload["app"] = look["app"] as Any
            if let jpeg = look["jpeg"] {
                payload["jpeg"] = jpeg
            }
        }
        return ok(payload, command: "ui_action", requestId: requestId)
    }

    private func keyboard(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard ensureAccessibility() else {
            return accessibilityDenied(command: "keyboard", requestId: requestId)
        }
        if let text = string(arguments, "text") ?? string(arguments, "value"), arguments["keys"] == nil {
            let typed = typeUnicode(text)
            return ok(["action": "type", "typed": typed, "spoken": typed ? "Typed." : "I couldn't type that."], command: "keyboard", requestId: requestId, ok: typed)
        }
        let keys = string(arguments, "keys") ?? string(arguments, "shortcut") ?? ""
        let posted = postHotkey(keys)
        return ok(
            ["action": "keyboard", "keys": keys, "spoken": posted ? "Sent." : "I couldn't send that shortcut."],
            command: "keyboard",
            requestId: requestId,
            ok: posted
        )
    }

    private func windowOp(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard ensureAccessibility() else {
            return accessibilityDenied(command: "window_op", requestId: requestId)
        }
        let op = (string(arguments, "op") ?? string(arguments, "action") ?? "raise").lowercased()
        let running = NSWorkspace.shared.frontmostApplication
        guard let running else {
            return fail("not_running", "No window is in front.", command: "window_op", requestId: requestId)
        }
        let appEl = AXUIElementCreateApplication(running.processIdentifier)
        let window = axElement(appEl, kAXFocusedWindowAttribute)
        guard let window else {
            return fail("not_found", "I couldn't find the front window.", command: "window_op", requestId: requestId)
        }
        var changed = false
        switch op {
        case "raise", "focus", "foreground":
            changed = AXUIElementPerformAction(window, kAXRaiseAction as CFString) == .success
            running.activate(options: [.activateAllWindows])
        case "close":
            if let button = axElement(window, kAXCloseButtonAttribute) {
                changed = AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
            }
        case "minimize":
            if let button = axElement(window, kAXMinimizeButtonAttribute) {
                changed = AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
            }
        case "zoom", "maximize":
            if let button = axElement(window, kAXZoomButtonAttribute) {
                changed = AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
            }
        default:
            return fail("unknown_command", "Unknown window action.", command: "window_op", requestId: requestId)
        }
        return ok(
            ["action": op, "app": running.localizedName as Any, "spoken": changed ? "Done." : "That window action failed."],
            command: "window_op",
            requestId: requestId,
            ok: changed
        )
    }

    // MARK: - Screen

    private func screenLook(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        if !CGPreflightScreenCaptureAccess() {
            _ = CGRequestScreenCaptureAccess()
            if !CGPreflightScreenCaptureAccess() {
                return fail(
                    "permission_denied",
                    "Mac screen vision needs Screen Recording permission enabled for EV.",
                    command: "screen_look",
                    requestId: requestId
                )
            }
        }
        let target = (string(arguments, "target") ?? "active_window").lowercased()
        let running: NSRunningApplication?
        if let resolved = resolve(name: string(arguments, "app"), bundleId: string(arguments, "bundle_id")) {
            running = resolved.running ?? NSRunningApplication.runningApplications(withBundleIdentifier: resolved.bundle).first
        } else {
            running = NSWorkspace.shared.frontmostApplication
        }
        var image: CGImage?
        var bounds = CGRect.zero
        var windowID: CGWindowID = 0
        if target == "display" || target == "screen" {
            image = CGWindowListCreateImage(.infinite, .optionOnScreenOnly, kCGNullWindowID, [.bestResolution])
            bounds = NSScreen.main?.frame ?? .zero
        } else if let running, let info = windowInfo(pid: running.processIdentifier) {
            windowID = info.id
            bounds = info.bounds
            image = CGWindowListCreateImage(.null, .optionIncludingWindow, windowID, [.boundsIgnoreFraming, .bestResolution])
        }
        guard let image, let jpeg = Self.jpeg(image) else {
            return fail("capture_failed", "I couldn't capture the window.", command: "screen_look", requestId: requestId)
        }
        let frameId = "frame_\(Int(Date().timeIntervalSince1970 * 1000))"
        frames[frameId] = FrameRecord(
            frameId: frameId,
            bundleId: running?.bundleIdentifier ?? "",
            windowID: windowID,
            bounds: bounds,
            capturedAt: Date()
        )
        if frames.count > 6 {
            let oldest = frames.values.min(by: { $0.capturedAt < $1.capturedAt })
            if let oldest { frames.removeValue(forKey: oldest.frameId) }
        }
        return ok(
            [
                "frame_id": frameId,
                "width": image.width,
                "height": image.height,
                "jpeg": jpeg,
                "app": running?.localizedName as Any,
                "bundle_id": running?.bundleIdentifier as Any,
                "window_id": Int(windowID),
                "window": windowInfo(pid: running?.processIdentifier ?? 0)?.title as Any,
                "spoken": "A current window observation was submitted as an image in this conversation. Describe only what you can actually see.",
            ],
            command: "screen_look",
            requestId: requestId
        )
    }

    // MARK: - AX helpers

    private func ensureAccessibility() -> Bool {
        if AXIsProcessTrusted() { return true }
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        if AXIsProcessTrusted() { return true }
        DispatchQueue.main.async {
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
                NSWorkspace.shared.open(url)
            }
        }
        return AXIsProcessTrusted()
    }

    private func accessibilityDenied(command: String, requestId: String) -> [String: Any] {
        var payload = fail(
            "accessibility_denied",
            "I can open apps, but macOS hasn't given me Accessibility access yet, so I can't operate their controls.",
            command: command,
            requestId: requestId
        )
        payload["settings_url"] = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        payload["next_step"] = "Enable EV in System Settings → Privacy & Security → Accessibility, then keep talking."
        payload["ax_process"] = processIdentity()
        payload["accessibility_permission"] = "denied"
        payload["generic_ui_control_ready"] = false
        return payload
    }

    private func requestAccessibility(requestId: String) -> [String: Any] {
        _ = ensureAccessibility()
        var payload = permissionSnapshot()
        let ready = payload["generic_ui_control_ready"] as? Bool == true
        payload["ok"] = ready
        payload["command"] = "request_accessibility"
        payload["request_id"] = requestId
        payload["spoken"] = ready
            ? "Accessibility is on. I can operate app controls now."
            : "Open System Settings → Privacy & Security → Accessibility, add EV, and toggle it on."
        return payload
    }

    private func processIdentity() -> [String: Any] {
        let bundle = Bundle.main
        return [
            "pid": ProcessInfo.processInfo.processIdentifier,
            "bundle_id": bundle.bundleIdentifier as Any,
            "executable": bundle.executablePath as Any,
            "name": bundle.object(forInfoDictionaryKey: "CFBundleName") as Any,
        ]
    }

    private func functionalAccessibilityProbe() -> [String: Any] {
        let target = NSWorkspace.shared.frontmostApplication
            ?? NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.finder").first
        guard let target else {
            return ["ok": false, "error": "no_front_app"]
        }
        let appEl = AXUIElementCreateApplication(target.processIdentifier)
        let window = axElement(appEl, kAXFocusedWindowAttribute) ?? axElements(appEl, kAXWindowsAttribute).first
        guard let window else {
            return [
                "ok": false,
                "error": "no_window",
                "app": target.localizedName as Any,
                "pid": target.processIdentifier,
            ]
        }
        let title = stringValue(window, kAXTitleAttribute) ?? ""
        var walked: [WalkItem] = []
        walk(window, depth: 0, deadline: Date().addingTimeInterval(0.6), maxWalk: 80, query: "", into: &walked)
        let useful = walked.contains { Self.interactive.contains($0.role) || $0.role == "AXTextArea" || $0.role == "AXTextField" || $0.role == "AXButton" }
        return [
            "ok": useful || !title.isEmpty,
            "app": target.localizedName as Any,
            "pid": target.processIdentifier,
            "window": title,
            "elements_found": walked.count,
            "method": "accessibility",
        ]
    }

    private struct WalkItem {
        let element: AXUIElement
        let role: String
        let subrole: String
        let title: String
        let value: String
        let identifier: String
        let enabled: Bool
        let focused: Bool
        let selected: Bool
        let secure: Bool
        let actions: [String]
        let score: Int
        let visible: Bool
    }

    private func walk(
        _ element: AXUIElement,
        depth: Int,
        deadline: Date,
        maxWalk: Int,
        query: String,
        into walked: inout [WalkItem],
        walkedCount: inout Int
    ) {
        if Date() > deadline || walkedCount >= maxWalk || depth > 14 { return }
        walkedCount += 1
        let role = stringValue(element, kAXRoleAttribute) ?? ""
        if Self.skipRoles.contains(role) && depth > 1 {
            return
        }
        let subrole = stringValue(element, kAXSubroleAttribute) ?? ""
        let title = firstNonEmpty(
            stringValue(element, kAXTitleAttribute),
            stringValue(element, kAXDescriptionAttribute),
            stringValue(element, "AXHelp")
        )
        let value = stringValue(element, kAXValueAttribute) ?? ""
        let identifier = stringValue(element, kAXIdentifierAttribute) ?? ""
        let enabled = boolValue(element, kAXEnabledAttribute, defaultValue: true)
        let focused = boolValue(element, kAXFocusedAttribute, defaultValue: false)
        let selected = boolValue(element, kAXSelectedAttribute, defaultValue: false)
        let secure = role == "AXSecureTextField" || subrole == "AXSecureTextField"
        let interactive = Self.interactive.contains(role)
            || (role == "AXStaticText" && (!value.isEmpty || !title.isEmpty))
            || (role == "AXGroup" && !title.isEmpty)
        if interactive {
            var actions: [String] = []
            var raw: CFArray?
            if AXUIElementCopyActionNames(element, &raw) == .success, let raw {
                actions = (raw as NSArray).compactMap { $0 as? String }
            }
            let score = matchScore(query: query, role: role, title: title, value: value, identifier: identifier, focused: focused)
            walked.append(
                WalkItem(
                    element: element,
                    role: role,
                    subrole: subrole,
                    title: title,
                    value: secure ? "" : String(value.prefix(120)),
                    identifier: identifier,
                    enabled: enabled,
                    focused: focused,
                    selected: selected,
                    secure: secure,
                    actions: actions,
                    score: score,
                    visible: isVisible(element)
                )
            )
        }
        let children = axElements(element, kAXChildrenAttribute)
        let budget = depth < 3 ? 60 : 28
        for child in children.prefix(budget) {
            walk(child, depth: depth + 1, deadline: deadline, maxWalk: maxWalk, query: query, into: &walked, walkedCount: &walkedCount)
            if Date() > deadline || walkedCount >= maxWalk { break }
        }
    }

    private func walk(
        _ element: AXUIElement,
        depth: Int,
        deadline: Date,
        maxWalk: Int,
        query: String,
        into walked: inout [WalkItem]
    ) {
        var count = 0
        walk(element, depth: depth, deadline: deadline, maxWalk: maxWalk, query: query, into: &walked, walkedCount: &count)
    }

    private func selectWalkItems(_ walked: [WalkItem], query: String, maxElements: Int) -> [WalkItem] {
        if walked.isEmpty { return [] }
        if !query.isEmpty {
            let matched = walked.filter { $0.score >= 50 }.sorted { $0.score > $1.score }
            if !matched.isEmpty { return Array(matched.prefix(maxElements)) }
        }
        let ranked = walked.sorted { lhs, rhs in
            let leftFocus = isFocusedElement(lhs.element)
            let rightFocus = isFocusedElement(rhs.element)
            if leftFocus != rightFocus { return leftFocus }
            if lhs.visible != rhs.visible { return lhs.visible }
            let leftPri = Self.priority(lhs.role)
            let rightPri = Self.priority(rhs.role)
            if leftPri != rightPri { return leftPri > rightPri }
            return lhs.score > rhs.score
        }
        return Array(ranked.prefix(maxElements))
    }

    private func scrollWalked(_ walked: [WalkItem]) -> Bool {
        var did = false
        for item in walked where item.role == "AXScrollArea" || item.role == "AXOutline" || item.role == "AXTable" {
            if AXUIElementPerformAction(item.element, "AXScrollDownByPage" as CFString) == .success {
                did = true
            }
        }
        return did
    }

    private func emit(
        _ item: WalkItem,
        pid: pid_t,
        bundleId: String,
        into collected: inout [[String: Any]],
        compact: inout [String]
    ) {
        let ref = "e\(generation)_\(collected.count + 1)"
        elements[ref] = ElementRecord(
            ref: ref,
            element: item.element,
            generation: generation,
            pid: pid,
            bundleId: bundleId,
            role: item.role,
            title: item.title,
            secure: item.secure
        )
        var payload: [String: Any] = [
            "ref": ref,
            "role": Self.shortRole(item.role),
            "ax_role": item.role,
            "title": item.title,
            "enabled": item.enabled,
            "focused": isFocusedElement(item.element),
            "selected": item.selected,
            "actions": item.actions,
            "secure": item.secure,
            "score": item.score,
        ]
        if !item.subrole.isEmpty { payload["subrole"] = item.subrole }
        if !item.identifier.isEmpty { payload["identifier"] = item.identifier }
        if !item.value.isEmpty, !item.secure { payload["value"] = item.value }
        if let point = axPoint(item.element), let size = axSize(item.element), size.width > 1, size.height > 1 {
            payload["x"] = Int(point.x)
            payload["y"] = Int(point.y)
        }
        collected.append(payload)
        var line = "\(Self.shortRole(item.role)): \(ref)"
        if !item.title.isEmpty { line += " \"\(item.title.prefix(80))\"" }
        if !item.value.isEmpty, !item.secure { line += " value=\(item.value.prefix(60))" }
        if isFocusedElement(item.element) { line += " focused" }
        if !item.enabled { line += " disabled" }
        compact.append(line)
    }

    private func matchScore(query: String, role: String, title: String, value: String, identifier: String, focused: Bool) -> Int {
        var score = 0
        if focused { score += 8 }
        if role == "AXTextArea" || role == "AXTextField" { score += 18 }
        if role == "AXButton" || role == "AXCheckBox" || role == "AXLink" { score += 12 }
        if query.isEmpty { return score }
        let hay = (title + " " + value + " " + identifier + " " + Self.shortRole(role)).lowercased()
        if hay == query { return score + 100 }
        if title.lowercased() == query { return score + 90 }
        if hay.contains(query) { score += 70 }
        let parts = query.split(separator: " ").map(String.init).filter { $0.count > 1 }
        score += parts.filter { hay.contains($0) }.count * 12
        if query.contains("text") && (role == "AXTextArea" || role == "AXTextField") { score += 40 }
        if query.contains("button") && role == "AXButton" { score += 30 }
        if query.contains("bluetooth") && hay.contains("bluetooth") { score += 50 }
        if query.contains("document") || query.contains("textarea") || query.contains("text area") || query.contains("editor") {
            if role == "AXTextArea" { score += 80 }
            else if role == "AXTextField" { score += 35 }
            else if role == "AXStaticText" { score -= 40 }
        }
        let wantsDiscard = (query.contains("don") && query.contains("save")) || query.contains("discard")
        if wantsDiscard {
            if hay.contains("don't save") || hay.contains("dont save") || hay.contains("delete") {
                score += 90
            }
        }
        return score
    }

    private func isVisible(_ element: AXUIElement) -> Bool {
        guard let point = axPoint(element), let size = axSize(element) else { return true }
        if size.width < 2 || size.height < 2 { return false }
        let screen = NSScreen.main?.frame ?? .infinite
        return screen.intersects(CGRect(origin: point, size: size))
    }

    private func axPoint(_ element: AXUIElement) -> CGPoint? {
        guard let value = copy(element, kAXPositionAttribute) else { return nil }
        var point = CGPoint.zero
        if AXValueGetValue(value as! AXValue, .cgPoint, &point) { return point }
        return nil
    }

    private func axSize(_ element: AXUIElement) -> CGSize? {
        guard let value = copy(element, kAXSizeAttribute) else { return nil }
        var size = CGSize.zero
        if AXValueGetValue(value as! AXValue, .cgSize, &size) { return size }
        return nil
    }

    private func setAXValue(_ element: AXUIElement, _ value: String) -> Bool {
        var settable: DarwinBoolean = false
        if AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable) == .success, settable.boolValue {
            return AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFTypeRef) == .success
        }
        return AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFTypeRef) == .success
    }

    private func openDownloads(requestId: String) -> [String: Any] {
        let url = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        guard let url else {
            return fail("not_found", "I couldn't find Downloads.", command: "open_app", requestId: requestId)
        }
        let opened = NSWorkspace.shared.open(url)
        Thread.sleep(forTimeInterval: 0.3)
        let finder = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.finder").first
        finder?.activate(options: [.activateAllWindows])
        lastBundle = "com.apple.finder"
        lastApp = "Finder"
        return ok(
            [
                "name": "Finder",
                "app": "Finder",
                "bundle_id": "com.apple.finder",
                "opened": opened,
                "path": url.path,
                "method": "native_api",
                "spoken": opened ? "Opened Downloads." : "I couldn't open Downloads.",
            ],
            command: "open_app",
            requestId: requestId,
            ok: opened
        )
    }

    private func activateBundle(_ bundleId: String) -> Bool {
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).first else {
            return false
        }
        app.unhide()
        app.activate(options: [.activateAllWindows])
        Thread.sleep(forTimeInterval: 0.18)
        return app.isActive || NSWorkspace.shared.frontmostApplication?.bundleIdentifier == bundleId
    }

    private func containsElement(_ items: [AXUIElement], _ target: AXUIElement) -> Bool {
        items.contains { CFEqual($0, target) }
    }

    private func isFocusedElement(_ element: AXUIElement) -> Bool {
        guard let focusedNow else { return false }
        return CFEqual(focusedNow, element)
    }

    private func isDialog(_ window: AXUIElement?) -> Bool {
        guard let window else { return false }
        let role = stringValue(window, kAXRoleAttribute) ?? ""
        let sub = stringValue(window, kAXSubroleAttribute) ?? ""
        if role == "AXSheet" || role == "AXDialog" || sub == "AXDialog" || sub == "AXSystemDialog" {
            return true
        }
        let title = (stringValue(window, kAXTitleAttribute) ?? "").lowercased()
        return title.contains("save") || title.contains("open") || title.contains("replace")
    }

    private func focusedTitle(pid: pid_t) -> String {
        let app = AXUIElementCreateApplication(pid)
        guard let focused = axElement(app, kAXFocusedUIElementAttribute) else { return "" }
        return firstNonEmpty(stringValue(focused, kAXTitleAttribute), stringValue(focused, kAXValueAttribute))
    }

    private func scroll(_ element: AXUIElement, _ arguments: [String: Any]) -> Bool {
        let direction = (string(arguments, "direction") ?? "down").lowercased()
        let action: String
        switch direction {
        case "up": action = "AXScrollUpByPage"
        case "left": action = "AXScrollLeftByPage"
        case "right": action = "AXScrollRightByPage"
        default: action = "AXScrollDownByPage"
        }
        if AXUIElementPerformAction(element, action as CFString) == .success { return true }
        let amount = direction == "up" || direction == "left" ? 3 : -3
        let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: Int32(amount), wheel2: 0, wheel3: 0)
        event?.post(tap: .cghidEventTap)
        return event != nil
    }

    private func typeIntoFocused(_ text: String, requestId: String) -> [String: Any] {
        return enterText(text, mode: "insert", element: nil, requestId: requestId)
    }

    /// Shared text engine: AX settable value → clipboard paste → keystrokes.
    private func enterText(
        _ text: String,
        mode: String,
        element: AXUIElement?,
        requestId: String
    ) -> [String: Any] {
        let trimmed = text
        if trimmed.isEmpty {
            return fail("missing_text", "There is no text to enter.", command: "ui_action", requestId: requestId)
        }
        if let lastBundle { _ = activateBundle(lastBundle) }
        var target = element
        if target == nil {
            if let app = NSWorkspace.shared.frontmostApplication {
                let appEl = AXUIElementCreateApplication(app.processIdentifier)
                target = axElement(appEl, kAXFocusedUIElementAttribute)
            }
        }
        if let target {
            _ = AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            Thread.sleep(forTimeInterval: 0.06)
        }
        let before = target.flatMap { stringValue($0, kAXValueAttribute) } ?? ""
        var method = "none"
        var changed = false
        if let target {
            if mode == "replace" {
                changed = setAXValue(target, trimmed)
                method = "ax_value"
            } else if mode == "append" {
                let joined = before.isEmpty ? trimmed : before + (trimmed.hasPrefix("\n") ? trimmed : "\n" + trimmed)
                changed = setAXValue(target, joined)
                method = "ax_value"
            } else {
                changed = setAXValue(target, before + trimmed)
                method = "ax_value"
            }
        }
        var after = target.flatMap { stringValue($0, kAXValueAttribute) } ?? ""
        if !changed || (mode != "replace" && !trimmed.isEmpty && !after.contains(String(trimmed.prefix(24)))) {
            changed = pasteText(trimmed, restore: true)
            method = "paste"
            Thread.sleep(forTimeInterval: 0.12)
            after = target.flatMap { stringValue($0, kAXValueAttribute) } ?? after
        }
        if !changed || (mode != "replace" && !trimmed.isEmpty && after == before && target != nil) {
            let toType = mode == "append" && !trimmed.hasPrefix("\n") ? "\n" + trimmed : trimmed
            changed = typeUnicode(toType)
            method = "keystroke"
            Thread.sleep(forTimeInterval: 0.08)
            after = target.flatMap { stringValue($0, kAXValueAttribute) } ?? after
        }
        let needle = String(trimmed.prefix(24))
        let verified = !needle.isEmpty && (after.contains(needle) || after.hasSuffix(trimmed) || after == trimmed)
        return ok(
            [
                "action": mode == "replace" ? "replace" : (mode == "append" ? "append" : "type"),
                "method": method,
                "executed": changed || verified,
                "verified": verified,
                "observed_text": String(after.prefix(240)),
                "spoken": verified
                    ? "Entered the text."
                    : (changed
                        ? "I triggered text entry, but I can't confirm the document changed."
                        : "I couldn't enter that text."),
            ],
            command: "ui_action",
            requestId: requestId,
            ok: changed || verified
        )
    }

    private func pasteText(_ text: String, restore: Bool) -> Bool {
        let board = NSPasteboard.general
        let prior = board.string(forType: .string)
        board.clearContents()
        let placed = board.setString(text, forType: .string)
        guard placed else { return false }
        let pasted = postHotkey("cmd+v")
        Thread.sleep(forTimeInterval: 0.08)
        if restore {
            board.clearContents()
            if let prior {
                board.setString(prior, forType: .string)
            }
        }
        return pasted
    }

    private func typeUnicode(_ text: String) -> Bool {
        guard !text.isEmpty else { return true }
        let parts = text.split(separator: "\n", omittingEmptySubsequences: false)
        var ok = true
        for (index, part) in parts.enumerated() {
            if !part.isEmpty {
                var utf16 = Array(String(part).utf16)
                let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
                event?.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: &utf16)
                event?.post(tap: .cghidEventTap)
                let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
                up?.post(tap: .cghidEventTap)
                ok = ok && event != nil
                Thread.sleep(forTimeInterval: 0.02)
            }
            if index < parts.count - 1 {
                ok = postHotkey("return") && ok
                Thread.sleep(forTimeInterval: 0.04)
            }
        }
        return ok
    }

    private func postHotkey(_ spec: String) -> Bool {
        let parts = spec.lowercased().split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
        guard let last = parts.last else { return false }
        var flags: CGEventFlags = []
        for part in parts.dropLast() {
            switch part {
            case "cmd", "command", "⌘": flags.insert(.maskCommand)
            case "shift": flags.insert(.maskShift)
            case "option", "alt": flags.insert(.maskAlternate)
            case "ctrl", "control": flags.insert(.maskControl)
            default: break
            }
        }
        guard let key = Self.keyCode(last) else { return false }
        let down = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true)
        down?.flags = flags
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false)
        up?.flags = flags
        up?.post(tap: .cghidEventTap)
        return true
    }

    private func click(_ point: CGPoint) {
        let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
        up?.post(tap: .cghidEventTap)
    }

    private func doubleClick(_ point: CGPoint) {
        func pulse(_ state: Int64) {
            let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
            down?.setIntegerValueField(.mouseEventClickState, value: state)
            down?.post(tap: .cghidEventTap)
            let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
            up?.setIntegerValueField(.mouseEventClickState, value: state)
            up?.post(tap: .cghidEventTap)
        }
        pulse(1)
        Thread.sleep(forTimeInterval: 0.04)
        pulse(2)
    }

    private func rightClick(_ point: CGPoint) {
        let down = CGEvent(mouseEventSource: nil, mouseType: .rightMouseDown, mouseCursorPosition: point, mouseButton: .right)
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(mouseEventSource: nil, mouseType: .rightMouseUp, mouseCursorPosition: point, mouseButton: .right)
        up?.post(tap: .cghidEventTap)
    }

    private func hidCenter(of element: AXUIElement) -> CGPoint? {
        guard let origin = axPoint(element), let size = axSize(element), size.width > 1, size.height > 1 else {
            return nil
        }
        let cocoa = CGPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2)
        return hidFromCocoa(cocoa)
    }

    private func hidFromCocoa(_ point: CGPoint) -> CGPoint {
        // Accessibility frames are top-left origin; CGEvent is bottom-left of the desktop.
        let height = NSScreen.screens.map { $0.frame.maxY }.max() ?? 0
        return CGPoint(x: point.x, y: height - point.y)
    }

    private func drag(_ arguments: [String: Any], from record: ElementRecord, requestId: String) -> [String: Any] {
        guard let start = hidCenter(of: record.element) else {
            return fail("no_frame", "I couldn't find that control's position to drag.", command: "ui_action", requestId: requestId)
        }
        guard let frameId = string(arguments, "frame_id"), let frame = frames[frameId] else {
            return fail("stale_frame", "I need a current screenshot before dragging to coordinates.", command: "ui_action", requestId: requestId)
        }
        if Date().timeIntervalSince(frame.capturedAt) > 8 {
            return fail("stale_frame", "That screenshot is too old to drag.", command: "ui_action", requestId: requestId)
        }
        let xNorm = double(arguments, "x_normalized") ?? double(arguments, "x") ?? -1
        let yNorm = double(arguments, "y_normalized") ?? double(arguments, "y") ?? -1
        guard xNorm >= 0, xNorm <= 1, yNorm >= 0, yNorm <= 1 else {
            return fail("bad_coordinates", "Drag destination must be normalized 0–1.", command: "ui_action", requestId: requestId)
        }
        let end = CGPoint(
            x: frame.bounds.minX + frame.bounds.width * xNorm,
            y: frame.bounds.minY + frame.bounds.height * yNorm
        )
        let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left)
        down?.post(tap: .cghidEventTap)
        let steps = 10
        for step in 1...steps {
            let t = CGFloat(step) / CGFloat(steps)
            let point = CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t)
            let dragged = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: point, mouseButton: .left)
            dragged?.post(tap: .cghidEventTap)
            Thread.sleep(forTimeInterval: 0.012)
        }
        let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: end, mouseButton: .left)
        up?.post(tap: .cghidEventTap)
        Thread.sleep(forTimeInterval: 0.12)
        return ok(
            [
                "action": "drag",
                "element_ref": record.ref,
                "frame_id": frameId,
                "x_normalized": xNorm,
                "y_normalized": yNorm,
                "method": "hid_drag",
                "ui_changed": true,
                "spoken": "Dragged.",
            ],
            command: "ui_action",
            requestId: requestId
        )
    }

    private func scrollFront(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        guard ensureAccessibility() else {
            return accessibilityDenied(command: "ui_action", requestId: requestId)
        }
        let direction = (string(arguments, "direction") ?? "down").lowercased()
        var changed = false
        if let app = NSWorkspace.shared.frontmostApplication {
            let appEl = AXUIElementCreateApplication(app.processIdentifier)
            if let focused = axElement(appEl, kAXFocusedUIElementAttribute) {
                changed = scroll(focused, arguments)
            }
            if !changed, let window = axElement(appEl, kAXFocusedWindowAttribute) {
                changed = scroll(window, arguments)
            }
        }
        if !changed {
            let amount: Int32 = (direction == "up" || direction == "left") ? 4 : -4
            let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: amount, wheel2: 0, wheel3: 0)
            event?.post(tap: .cghidEventTap)
            changed = event != nil
        }
        return ok(
            ["action": "scroll", "direction": direction, "ui_changed": changed, "spoken": changed ? "Scrolled." : "I couldn't scroll."],
            command: "ui_action",
            requestId: requestId,
            ok: changed
        )
    }

    // MARK: - Semantic app adapters (Music)

    static func adapterControl(app: String?, bundleId: String?) -> [String: Any] {
        let name = (app ?? "").lowercased()
        let bundle = (bundleId ?? "").lowercased()
        if name.contains("music") || bundle == "com.apple.music" {
            return [
                "preferred": "semantic_adapter",
                "semantic_adapter": "music",
                "supported_actions": ["find_playlist", "list_tracks", "play", "play_playlist_track", "pause", "next", "previous", "status"],
                "fallbacks": ["accessibility", "screen_vision", "coordinate"],
                "verification": "semantic_player_state",
            ]
        }
        if name.contains("safari") || bundle == "com.apple.safari" {
            return [
                "preferred": "semantic_adapter",
                "semantic_adapter": "safari",
                "supported_actions": ["search", "navigate", "play", "status", "open_item"],
                "fallbacks": ["accessibility", "keyboard", "screen_vision", "coordinate"],
                "verification": "player_or_url",
            ]
        }
        if name.contains("notes") || bundle == "com.apple.notes" {
            return [
                "preferred": "semantic_adapter",
                "semantic_adapter": "notes",
                "supported_actions": ["create", "append", "read", "status"],
                "fallbacks": ["accessibility", "keyboard", "screen_vision"],
                "verification": "note_body",
            ]
        }
        if name.contains("finder") || bundle == "com.apple.finder" {
            return [
                "preferred": "semantic_adapter",
                "semantic_adapter": "finder",
                "supported_actions": ["play", "open_item", "open_folder", "status"],
                "fallbacks": ["accessibility", "keyboard", "screen_vision"],
                "verification": "player_or_selection",
            ]
        }
        if name.contains("chrome") || bundle == "com.google.Chrome" {
            return [
                "preferred": "semantic_adapter",
                "semantic_adapter": "chrome",
                "supported_actions": ["search", "navigate", "play", "status", "open_item"],
                "fallbacks": ["accessibility", "keyboard", "screen_vision", "coordinate"],
                "verification": "player_or_url",
            ]
        }
        if name.contains("spotify") || bundle == "com.spotify.client" {
            return [
                "preferred": "semantic_adapter",
                "semantic_adapter": "spotify",
                "supported_actions": ["play", "pause", "next", "previous", "status", "search", "open_item"],
                "fallbacks": ["accessibility", "keyboard", "screen_vision", "coordinate"],
                "verification": "now_playing",
            ]
        }
        return [
            "preferred": "accessibility",
            "semantic_adapter": NSNull(),
            "supported_actions": [],
            "fallbacks": ["accessibility", "keyboard", "screen_vision", "coordinate"],
            "verification": "inspect_ui_or_screen",
        ]
    }

    private func appAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let app = (string(arguments, "app") ?? lastApp ?? "")
        let lower = app.lowercased()
        if lower.contains("music") || lower == "itunes" {
            return musicAction(arguments, requestId: requestId)
        }
        if lower.contains("safari") {
            return safariAction(arguments, requestId: requestId)
        }
        if lower.contains("notes") {
            return notesAction(arguments, requestId: requestId)
        }
        if lower.contains("finder") {
            return finderAction(arguments, requestId: requestId)
        }
        if lower.contains("calculator") || lower == "calc" {
            return calculatorAction(arguments, requestId: requestId)
        }
        if lower.contains("chrome") {
            return chromeAction(arguments, requestId: requestId)
        }
        if lower.contains("spotify") {
            return spotifyAction(arguments, requestId: requestId)
        }
        if looksLikeLocalVideoPlayer(name: lower, bundle: "") {
            let action = (string(arguments, "action") ?? "play").lowercased()
            if action == "play" || action == "open_item" {
                return finderPlayVideo(
                    query: string(arguments, "query") ?? string(arguments, "value") ?? "",
                    requestId: requestId
                )
            }
        }
        return genericAppAction(arguments, requestId: requestId)
    }

    private func genericAppAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "status").lowercased()
        let rawApp = string(arguments, "app") ?? ""
        let useFront = rawApp.isEmpty || ["front", "this", "that", "it"].contains(rawApp.lowercased())
        if !useFront {
            let opened = openApp(["name": rawApp], requestId: requestId, activate: true)
            if opened["ok"] as? Bool == false {
                return opened
            }
            Thread.sleep(forTimeInterval: 0.2)
        }
        var query = Self.cleanedComputerQuery(
            string(arguments, "query")
                ?? string(arguments, "value")
                ?? string(arguments, "text")
                ?? ""
        )
        if !rawApp.isEmpty {
            let escaped = NSRegularExpression.escapedPattern(for: rawApp)
            query = query.replacingOccurrences(
                of: #"(?i)\s+(?:in|on|using|with)\s+\#(escaped)\s*$"#,
                with: "",
                options: .regularExpression
            )
        }
        let inspectArgs: [String: Any] = useFront ? [:] : ["app": rawApp]
        switch action {
        case "status", "read":
            var snap = inspectUI(inspectArgs, requestId: requestId)
            snap["adapter"] = "generic"
            snap["action"] = action
            snap["executed"] = true
            snap["verified"] = snap["ok"] as? Bool == true
            if (snap["spoken"] as? String)?.isEmpty != false {
                snap["spoken"] = "I'm looking at \(snap["app"] as? String ?? "the app")."
            }
            return snap
        case "new_tab", "close_tab", "next_tab", "previous_tab":
            let spec: String
            let spoken: String
            switch action {
            case "new_tab":
                spec = "cmd+t"
                spoken = "Opened a new tab."
            case "close_tab":
                spec = "cmd+w"
                spoken = "Closed the tab."
            case "next_tab":
                spec = "cmd+shift+]"
                spoken = "Switched to the next tab."
            default:
                spec = "cmd+shift+["
                spoken = "Switched to the previous tab."
            }
            let posted = postHotkey(spec)
            Thread.sleep(forTimeInterval: 0.2)
            return ok(
                [
                    "ok": posted,
                    "executed": true,
                    "verified": posted,
                    "adapter": "generic",
                    "app": rawApp.isEmpty ? (NSWorkspace.shared.frontmostApplication?.localizedName ?? "front") : rawApp,
                    "action": action,
                    "method": "keyboard",
                    "spoken": posted ? spoken : "I couldn't send that tab shortcut.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: posted
            )
        case "search", "create", "append":
            if query.isEmpty {
                return fail("missing_query", "What should I type?", command: "app_action", requestId: requestId)
            }
            let fieldQuery = action == "search" ? "search" : ""
            let inspect = inspectUI(inspectArgs.merging(["query": fieldQuery]) { _, new in new }, requestId: requestId)
            let typed: [String: Any]
            if let elements = inspect["elements"] as? [[String: Any]],
               let ref = elements.first?["ref"] as? String
            {
                typed = uiAction(
                    [
                        "action": action == "append" ? "append" : (action == "search" ? "replace" : "type"),
                        "element_ref": ref,
                        "value": query,
                    ],
                    requestId: requestId
                )
            } else {
                typed = enterText(query, mode: action == "append" ? "append" : "insert", element: nil, requestId: requestId)
            }
            if action == "search" {
                _ = postHotkey("return")
                Thread.sleep(forTimeInterval: 0.35)
            }
            let okTyped = typed["ok"] as? Bool == true
            return ok(
                [
                    "ok": okTyped,
                    "executed": true,
                    "verified": okTyped,
                    "adapter": "generic",
                    "app": inspect["app"] as Any,
                    "action": action,
                    "query": query,
                    "method": "accessibility",
                    "spoken": okTyped
                        ? (action == "search" ? "Searched for \(query)." : "Typed that.")
                        : "I couldn't type in this app.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: okTyped
            )
        default:
            return genericClickVisible(
                query: query,
                inspectArgs: inspectArgs,
                requestId: requestId
            )
        }
    }

    private func genericClickVisible(
        query: String,
        inspectArgs: [String: Any],
        requestId: String
    ) -> [String: Any] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let frontName = (NSWorkspace.shared.frontmostApplication?.localizedName ?? "").lowercased()
        let lowerNeedle = needle.lowercased()
        let wantsMedia = lowerNeedle.contains("first video")
            || lowerNeedle.contains("play the first")
            || (lowerNeedle.contains("video") && (lowerNeedle.contains("first") || lowerNeedle.contains("play")))
        if wantsMedia {
            if frontName.contains("safari") {
                return playMediaInBrowser(browser: "safari", arguments: ["query": needle], requestId: requestId)
            }
            if frontName.contains("chrome") {
                return playMediaInBrowser(browser: "chrome", arguments: ["query": needle], requestId: requestId)
            }
            if frontName.contains("finder") {
                return finderPlayVideo(query: needle, requestId: requestId)
            }
            if looksLikeLocalVideoPlayer(name: frontName, bundle: NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "") {
                return finderPlayVideo(query: needle, requestId: requestId)
            }
        }
        if lowerNeedle.contains("first result")
            || lowerNeedle.contains("top result")
            || lowerNeedle.contains("first search result")
            || lowerNeedle.contains("first item")
            || lowerNeedle.contains("first one")
            || lowerNeedle == "first" {
            if frontName.contains("safari") {
                return safariOpenFirstResult(requestId: requestId)
            }
            if frontName.contains("chrome") {
                return chromeOpenFirstResult([:], requestId: requestId)
            }
        }
        let inspect = inspectUI(
            inspectArgs.merging(needle.isEmpty ? [:] : ["query": needle]) { _, new in new },
            requestId: requestId
        )
        if let elements = inspect["elements"] as? [[String: Any]],
           let first = elements.first,
           let ref = first["ref"] as? String
        {
            let pressed = uiAction(["action": "press", "element_ref": ref], requestId: requestId)
            let okPress = pressed["ok"] as? Bool == true
            return ok(
                [
                    "ok": okPress,
                    "executed": true,
                    "verified": okPress,
                    "adapter": "generic",
                    "app": inspect["app"] as Any,
                    "action": "open_item",
                    "query": needle,
                    "element_ref": ref,
                    "method": "accessibility",
                    "spoken": okPress
                        ? "Clicked \(first["title"] as? String ?? needle)."
                        : "I found that control but couldn't press it.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: okPress
            )
        }
        if !needle.isEmpty, let hit = clickVisibleText(needle) {
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": true,
                    "adapter": "generic",
                    "app": inspect["app"] as Any,
                    "action": "open_item",
                    "query": needle,
                    "method": "screen_ocr",
                    "matched_text": hit,
                    "spoken": "Clicked \(hit).",
                ],
                command: "app_action",
                requestId: requestId
            )
        }
        return ok(
            [
                "ok": false,
                "executed": true,
                "verified": false,
                "adapter": "generic",
                "app": inspect["app"] as Any,
                "action": "open_item",
                "query": needle,
                "method": "failed",
                "spoken": needle.isEmpty
                    ? "I can see the window, but I need a control to click."
                    : "I didn't find “\(needle)” on screen.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: false
        )
    }

    private func clickVisibleText(_ needle: String) -> String? {
        guard CGPreflightScreenCaptureAccess() else { return nil }
        let running = NSWorkspace.shared.frontmostApplication
        guard let running, let info = windowInfo(pid: running.processIdentifier) else { return nil }
        guard let image = CGWindowListCreateImage(
            .null,
            .optionIncludingWindow,
            info.id,
            [.boundsIgnoreFraming, .bestResolution]
        ) else { return nil }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do {
            try handler.perform([request])
        } catch {
            return nil
        }
        let want = needle.lowercased()
        var best: (text: String, box: CGRect, score: Int)?
        for observation in request.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string
            let lower = text.lowercased()
            var score = 0
            if lower == want { score = 100 }
            else if lower.contains(want) { score = 80 }
            else if want.contains(lower), lower.count >= 3 { score = 60 }
            else { continue }
            if best == nil || score > best!.score {
                best = (text, observation.boundingBox, score)
            }
        }
        guard let best else { return nil }
        let imgW = CGFloat(image.width)
        let imgH = CGFloat(image.height)
        let visionRect = VNImageRectForNormalizedRect(best.box, image.width, image.height)
        let xNorm = visionRect.midX / imgW
        let yNorm = 1 - (visionRect.midY / imgH)
        let point = CGPoint(
            x: info.bounds.minX + info.bounds.width * xNorm,
            y: info.bounds.minY + info.bounds.height * yNorm
        )
        click(point)
        Thread.sleep(forTimeInterval: 0.2)
        return best.text
    }

    private func musicAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "status").lowercased()
        switch action {
        case "status", "current", "now_playing":
            return musicStatus(requestId: requestId, spokenPrefix: nil)
        case "pause":
            _ = runAppleScript("tell application id \"com.apple.Music\" to pause")
            Thread.sleep(forTimeInterval: 0.25)
            return musicStatus(requestId: requestId, spokenPrefix: "Paused.")
        case "next":
            _ = runAppleScript("tell application id \"com.apple.Music\" to next track")
            Thread.sleep(forTimeInterval: 0.35)
            return musicStatus(requestId: requestId, spokenPrefix: nil)
        case "previous", "back":
            _ = runAppleScript("tell application id \"com.apple.Music\" to previous track")
            Thread.sleep(forTimeInterval: 0.35)
            return musicStatus(requestId: requestId, spokenPrefix: nil)
        case "find_playlist", "search", "list_playlists":
            return musicFindPlaylist(arguments, requestId: requestId)
        case "list_tracks":
            return musicListTracks(arguments, requestId: requestId)
        case "play", "play_track", "play_playlist", "play_playlist_track":
            return musicPlay(arguments, requestId: requestId)
        default:
            return fail("unknown_action", "Music cannot do that.", command: "app_action", requestId: requestId)
        }
    }

    private func asLiteral(_ value: String) -> String {
        let escaped = value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }

    private func runAppleScript(_ source: String, timeout: TimeInterval = 8) -> (ok: Bool, text: String, error: String?) {
        final class Box: @unchecked Sendable {
            var ok = false
            var text = ""
            var error: String?
        }
        let box = Box()
        let lock = NSLock()
        let done = DispatchSemaphore(value: 0)
        let execute = {
            var error: NSDictionary?
            if let script = NSAppleScript(source: source) {
                let result = script.executeAndReturnError(&error)
                lock.lock()
                if let error {
                    box.ok = false
                    box.error = (error[NSAppleScript.errorMessage] as? String) ?? "AppleScript failed"
                } else {
                    box.ok = true
                    box.text = result.stringValue ?? ""
                }
                lock.unlock()
            } else {
                lock.lock()
                box.error = "AppleScript compile failed"
                lock.unlock()
            }
            done.signal()
        }
        // NSAppleScript needs the AppKit run loop. Background threads hang
        // until timeout while Talk's audio engines are up; e2e without mic
        // happened to succeed on a global queue.
        if Thread.isMainThread {
            execute()
        } else {
            DispatchQueue.main.async(execute: execute)
        }
        if done.wait(timeout: .now() + min(timeout, 4)) == .timedOut {
            return runOsascriptProcess(source, timeout: timeout)
        }
        lock.lock()
        let out = (box.ok, box.text, box.error)
        lock.unlock()
        return out
    }

    /// Separate process so a stuck NSAppleScript run loop cannot block Talk.
    private func runOsascriptProcess(_ source: String, timeout: TimeInterval) -> (ok: Bool, text: String, error: String?) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        let input = Pipe()
        let output = Pipe()
        let err = Pipe()
        proc.standardInput = input
        proc.standardOutput = output
        proc.standardError = err
        do {
            try proc.run()
        } catch {
            return (false, "", error.localizedDescription)
        }
        input.fileHandleForWriting.write(Data(source.utf8))
        try? input.fileHandleForWriting.close()
        let deadline = Date().addingTimeInterval(timeout)
        while proc.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if proc.isRunning {
            proc.terminate()
            return (false, "", "timeout")
        }
        let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let errText = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if proc.terminationStatus == 0 {
            return (true, text, nil)
        }
        return (false, "", errText.isEmpty ? "osascript failed" : errText)
    }

    private func musicPlaylistNames() -> (ok: Bool, names: [String], error: String?) {
        let script = """
        tell application id "com.apple.Music"
          set out to ""
          repeat with p in playlists
            set out to out & name of p & linefeed
          end repeat
          return out
        end tell
        """
        let ran = runAppleScript(script)
        if !ran.ok {
            return (false, [], ran.error)
        }
        let names = ran.text
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return (true, names, nil)
    }

    private func matchPlaylist(query: String, names: [String]) -> (matched: String?, candidates: [String], error: String?) {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return (nil, names, "missing_playlist") }
        let lower = needle.lowercased()
        let exact = names.filter { $0.lowercased() == lower }
        if exact.count == 1 { return (exact[0], exact, nil) }
        if exact.count > 1 { return (nil, exact, "ambiguous") }
        let partial = names.filter { $0.lowercased().contains(lower) }
        if partial.count == 1 { return (partial[0], partial, nil) }
        if partial.count > 1 { return (nil, partial, "ambiguous") }
        return (nil, [], "playlist_not_found")
    }

    private func musicFindPlaylist(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let listed = musicPlaylistNames()
        if !listed.ok {
            return musicScriptFail(listed.error, requestId: requestId)
        }
        let query = string(arguments, "playlist") ?? string(arguments, "query") ?? lastPlaylist ?? ""
        if query.isEmpty {
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": false,
                    "adapter": "music",
                    "app": "Music",
                    "action": "list_playlists",
                    "playlists": listed.names,
                    "spoken": "I listed \(listed.names.count) playlists.",
                ],
                command: "app_action",
                requestId: requestId
            )
        }
        let match = matchPlaylist(query: query, names: listed.names)
        if let name = match.matched {
            lastPlaylist = name
            let listedTracks = musicTrackRows(playlist: name, limit: 8)
            let tracks = listedTracks.tracks
            let firstName = (tracks.first?["name"] as? String) ?? ""
            var spoken = "Found playlist \(name)."
            if !firstName.isEmpty {
                spoken = "Found playlist \(name). The first track is \(firstName)."
            }
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": !tracks.isEmpty,
                    "adapter": "music",
                    "app": "Music",
                    "action": "find_playlist",
                    "playlist": name,
                    "playlists": match.candidates,
                    "tracks": tracks,
                    "track": firstName,
                    "index": tracks.isEmpty ? NSNull() : 1,
                    "method": "scripting_bridge",
                    "spoken": spoken,
                ],
                command: "app_action",
                requestId: requestId
            )
        }
        if match.error == "ambiguous" {
            let spoken = "Which playlist — " + match.candidates.prefix(4).joined(separator: " or ") + "?"
            return ok(
                [
                    "ok": false,
                    "executed": true,
                    "verified": false,
                    "error": "ambiguous",
                    "adapter": "music",
                    "app": "Music",
                    "candidates": match.candidates,
                    "spoken": spoken,
                ],
                command: "app_action",
                requestId: requestId,
                ok: false
            )
        }
        return ok(
            [
                "ok": false,
                "executed": true,
                "verified": false,
                "error": "playlist_not_found",
                "adapter": "music",
                "app": "Music",
                "playlist": query,
                "playlists": listed.names.prefix(20).map { $0 },
                "spoken": "I couldn't find a playlist named \(query).",
            ],
            command: "app_action",
            requestId: requestId,
            ok: false
        )
    }

    private func resolvedPlaylistName(_ arguments: [String: Any]) -> (name: String?, error: String?, candidates: [String]) {
        let listed = musicPlaylistNames()
        if !listed.ok {
            return (nil, listed.error, [])
        }
        let query = string(arguments, "playlist") ?? string(arguments, "query") ?? lastPlaylist ?? ""
        if query.isEmpty {
            return (nil, "missing_playlist", listed.names)
        }
        let match = matchPlaylist(query: query, names: listed.names)
        return (match.matched, match.error, match.candidates)
    }

    private func musicListTracks(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let resolved = resolvedPlaylistName(arguments)
        if let err = resolved.error, resolved.name == nil {
            if err == "ambiguous" {
                return ok(
                    [
                        "ok": false,
                        "executed": true,
                        "verified": false,
                        "error": "ambiguous",
                        "candidates": resolved.candidates,
                        "spoken": "Which playlist — " + resolved.candidates.prefix(4).joined(separator: " or ") + "?",
                        "adapter": "music",
                        "app": "Music",
                    ],
                    command: "app_action",
                    requestId: requestId,
                    ok: false
                )
            }
            if err == "playlist_not_found" || err == "missing_playlist" {
                return ok(
                    [
                        "ok": false,
                        "executed": true,
                        "verified": false,
                        "error": err == "missing_playlist" ? "missing_playlist" : "playlist_not_found",
                        "spoken": err == "missing_playlist" ? "Which playlist?" : "I couldn't find that playlist.",
                        "adapter": "music",
                        "app": "Music",
                    ],
                    command: "app_action",
                    requestId: requestId,
                    ok: false
                )
            }
            return musicScriptFail(err, requestId: requestId)
        }
        guard let playlist = resolved.name else {
            return musicScriptFail("missing_playlist", requestId: requestId)
        }
        lastPlaylist = playlist
        let listedTracks = musicTrackRows(playlist: playlist, limit: 40)
        if !listedTracks.ok {
            return musicScriptFail(listedTracks.error, requestId: requestId)
        }
        let tracks = listedTracks.tracks
        return ok(
            [
                "ok": true,
                "executed": true,
                "verified": true,
                "adapter": "music",
                "app": "Music",
                "playlist": playlist,
                "tracks": tracks,
                "action": "list_tracks",
                "spoken": "Playlist \(playlist) has tracks listed.",
            ],
            command: "app_action",
            requestId: requestId
        )
    }

    private func musicTrackRows(playlist: String, limit: Int) -> (ok: Bool, tracks: [[String: Any]], error: String?) {
        let script = """
        tell application id "com.apple.Music"
          set out to ""
          set i to 0
          repeat with t in tracks of playlist \(asLiteral(playlist))
            set i to i + 1
            set out to out & i & tab & name of t & linefeed
            if i ≥ \(max(1, limit)) then exit repeat
          end repeat
          return out
        end tell
        """
        let ran = runAppleScript(script)
        if !ran.ok { return (false, [], ran.error) }
        var tracks: [[String: Any]] = []
        for line in ran.text.split(whereSeparator: \.isNewline) {
            let parts = line.split(separator: "\t", maxSplits: 1).map(String.init)
            guard parts.count == 2, let index = Int(parts[0]) else { continue }
            tracks.append(["index": index, "name": parts[1]])
        }
        return (true, tracks, nil)
    }

    private func musicPlay(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let resolved = resolvedPlaylistName(arguments)
        if let err = resolved.error, resolved.name == nil {
            if err == "ambiguous" {
                return ok(
                    [
                        "ok": false,
                        "executed": true,
                        "verified": false,
                        "error": "ambiguous",
                        "candidates": resolved.candidates,
                        "adapter": "music",
                        "app": "Music",
                        "spoken": "Which playlist — " + resolved.candidates.prefix(4).joined(separator: " or ") + "?",
                    ],
                    command: "app_action",
                    requestId: requestId,
                    ok: false
                )
            }
            if err == "playlist_not_found" || err == "missing_playlist" {
                return ok(
                    [
                        "ok": false,
                        "executed": true,
                        "verified": false,
                        "error": err == "missing_playlist" ? "missing_playlist" : "playlist_not_found",
                        "adapter": "music",
                        "app": "Music",
                        "playlist": string(arguments, "playlist") as Any,
                        "spoken": err == "missing_playlist"
                            ? "Which playlist should I play?"
                            : "I couldn't find a playlist named \(string(arguments, "playlist") ?? "that").",
                    ],
                    command: "app_action",
                    requestId: requestId,
                    ok: false
                )
            }
            return musicScriptFail(err, requestId: requestId)
        }
        guard let playlist = resolved.name else {
            return musicScriptFail("missing_playlist", requestId: requestId)
        }
        var index = int(arguments, "index") ?? lastTrackIndex ?? 1
        if index == 0 { index = 1 }
        lastPlaylist = playlist
        let countScript = "tell application id \"com.apple.Music\" to count tracks of playlist \(asLiteral(playlist))"
        let counted = runAppleScript(countScript)
        let total = Int(counted.text.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0
        if index < 0 { index = max(total, 1) }
        if total > 0 && index > total {
            return ok(
                [
                    "ok": false,
                    "executed": true,
                    "verified": false,
                    "error": "track_unavailable",
                    "adapter": "music",
                    "app": "Music",
                    "playlist": playlist,
                    "index": index,
                    "spoken": "Playlist \(playlist) does not have a track \(index).",
                ],
                command: "app_action",
                requestId: requestId,
                ok: false
            )
        }
        let nameScript = "tell application id \"com.apple.Music\" to get name of track \(index) of playlist \(asLiteral(playlist))"
        let named = runAppleScript(nameScript)
        let wanted = named.ok ? named.text.trimmingCharacters(in: .whitespacesAndNewlines) : ""
        let playScript = """
        tell application id "com.apple.Music"
          activate
          play track \(index) of playlist \(asLiteral(playlist))
        end tell
        """
        let played = runAppleScript(playScript)
        if !played.ok {
            return musicScriptFail(played.error, requestId: requestId)
        }
        lastTrackIndex = index
        Thread.sleep(forTimeInterval: 0.5)
        var status = musicNowPlaying()
        if status.state != "playing" || (!wanted.isEmpty && status.track.lowercased() != wanted.lowercased()) {
            Thread.sleep(forTimeInterval: 0.9)
            status = musicNowPlaying()
        }
        let verified = status.state == "playing" && (wanted.isEmpty || status.track.lowercased() == wanted.lowercased())
        let spoken: String
        if verified {
            spoken = "Playing \(status.track)."
        } else if status.state == "playing" {
            spoken = "Music is playing \(status.track), which is not the requested track."
        } else {
            spoken = "I told Music to play \(wanted.isEmpty ? "that track" : wanted), but playback is \(status.state)."
        }
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "adapter": "music",
                "app": "Music",
                "playlist": playlist,
                "index": index,
                "track": verified ? status.track : wanted,
                "observed_track": status.track,
                "artist": status.artist,
                "player_state": status.state,
                "method": "scripting_bridge",
                "action": "play",
                "spoken": spoken,
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func safariAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "status").lowercased()
        if let chrome = safariBrowserChrome(action, requestId: requestId) {
            return chrome
        }
        switch action {
        case "status", "read":
            return safariStatus(requestId: requestId, spokenPrefix: nil)
        case "search":
            let query = searchQuery(arguments)
            if query.isEmpty {
                return fail("missing_query", "What should I search for?", command: "app_action", requestId: requestId)
            }
            if let dest = Self.navigationURL(from: query) {
                return safariOpenLocation(dest, requestId: requestId)
            }
            lastSafariQuery = query
            let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
            let url = "https://www.google.com/search?q=\(encoded)"
            let script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then
                make new document
              else
                tell window 1
                  set current tab to (make new tab at end of tabs)
                end tell
              end if
              set URL of current tab of window 1 to \(asLiteral(url))
            end tell
            """
            let ran = runAppleScript(script)
            if !ran.ok {
                return fail("safari_search_failed", ran.error ?? "Safari search failed.", command: "app_action", requestId: requestId)
            }
            Thread.sleep(forTimeInterval: 1.8)
            let status = safariNow()
            let loaded = !status.url.isEmpty
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": loaded,
                    "must_continue": !loaded,
                    "adapter": "safari",
                    "app": "Safari",
                    "action": "search",
                    "query": query,
                    "url": status.url,
                    "title": status.title,
                    "spoken": loaded
                        ? "Safari is showing search results for \(query)."
                        : "Safari did not load the search yet.",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "navigate", "open_item":
            let query = locationQuery(arguments)
            if let dest = Self.navigationURL(from: query) {
                return safariOpenLocation(dest, requestId: requestId)
            }
            if queryLooksLikeMedia(query) {
                return playMediaInBrowser(browser: "safari", arguments: arguments, requestId: requestId)
            }
            return safariOpenFirstResult(requestId: requestId, query: query)
        case "play":
            return playMediaInBrowser(browser: "safari", arguments: arguments, requestId: requestId)
        default:
            return fail("unknown_action", "Safari cannot do that.", command: "app_action", requestId: requestId)
        }
    }

    private func safariOpenLocation(_ url: String, requestId: String) -> [String: Any] {
        let script = """
        tell application id "com.apple.Safari"
          if (count of windows) is 0 then
            make new document
            set URL of current tab of window 1 to \(asLiteral(url))
          else
            tell window 1
              set current tab to (make new tab at end of tabs)
              set URL of current tab to \(asLiteral(url))
            end tell
          end if
        end tell
        """
        var ran = runAppleScript(script)
        if !ran.ok {
            ran = runAppleScript("tell application id \"com.apple.Safari\" to open location \(asLiteral(url))")
        }
        if !ran.ok {
            return fail("safari_navigate_failed", ran.error ?? "Safari could not open that page.", command: "app_action", requestId: requestId)
        }
        let host = URL(string: url)?.host?.lowercased() ?? ""
        Thread.sleep(forTimeInterval: 0.4)
        var status = safariNow()
        for _ in 0..<16 {
            let current = status.url.lowercased()
            let ready = !current.isEmpty
                && !current.hasPrefix("favorites:")
                && !current.hasPrefix("about:")
                && (host.isEmpty || current.contains(host))
            if ready { break }
            Thread.sleep(forTimeInterval: 0.35)
            status = safariNow()
        }
        let loaded = !status.url.isEmpty && !status.url.lowercased().hasPrefix("favorites:")
        let stillSearch = status.url.lowercased().contains("google.com/search")
        let hostOk = host.isEmpty || status.url.lowercased().contains(host)
        let verified = loaded && !stillSearch && hostOk
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "must_continue": !verified,
                "adapter": "safari",
                "app": "Safari",
                "action": "navigate",
                "url": status.url,
                "title": status.title,
                "goal_complete": verified,
                "spoken": verified
                    ? "Opened \(status.title.isEmpty ? status.url : status.title)."
                    : "Safari did not open that page.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func safariBrowserChrome(_ action: String, requestId: String) -> [String: Any]? {
        let script: String
        let spoken: String
        switch action {
        case "new_tab":
            script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then
                make new document
              else
                tell window 1
                  set current tab to (make new tab at end of tabs)
                end tell
              end if
            end tell
            """
            spoken = "Opened a new Safari tab."
        case "close_tab":
            script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then return
              tell window 1
                if (count of tabs) <= 1 then
                  close
                else
                  close current tab
                end if
              end tell
            end tell
            """
            spoken = "Closed the Safari tab."
        case "next_tab":
            script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then return
              tell window 1
                set i to index of current tab
                set n to count of tabs
                if n is 0 then return
                if i < n then
                  set current tab to tab (i + 1)
                else
                  set current tab to tab 1
                end if
              end tell
            end tell
            """
            spoken = "Switched to the next Safari tab."
        case "previous_tab":
            script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then return
              tell window 1
                set i to index of current tab
                set n to count of tabs
                if n is 0 then return
                if i > 1 then
                  set current tab to tab (i - 1)
                else
                  set current tab to tab n
                end if
              end tell
            end tell
            """
            spoken = "Switched to the previous Safari tab."
        default:
            return nil
        }
        let ran = runAppleScript(script)
        Thread.sleep(forTimeInterval: 0.35)
        let status = safariNow()
        return ok(
            [
                "ok": ran.ok,
                "executed": true,
                "verified": ran.ok,
                "adapter": "safari",
                "app": "Safari",
                "action": action,
                "url": status.url,
                "title": status.title,
                "must_continue": false,
                "goal_complete": ran.ok,
                "spoken": ran.ok ? spoken : "Safari could not do that tab action.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: ran.ok
        )
    }

    private func safariNow() -> (url: String, title: String) {
        let script = """
        tell application id "com.apple.Safari"
          if (count of windows) is 0 then return "||"
          set theTab to current tab of window 1
          return (URL of theTab) & "||" & (name of theTab)
        end tell
        """
        let ran = runAppleScript(script)
        let parts = ran.text.components(separatedBy: "||")
        return (parts.first ?? "", parts.count > 1 ? parts[1] : "")
    }

    private static let googleFirstResultJS = """
    (() => {
      const unwrap = (h) => {
        try {
          const u = new URL(h);
          if (u.hostname.indexOf('google.') !== -1 && (u.pathname === '/url' || u.pathname.indexOf('/url') === 0)) {
            return u.searchParams.get('q') || u.searchParams.get('url') || h;
          }
        } catch (e) {}
        return h;
      };
      const bad = (h) => {
        const s = (h || '').toLowerCase();
        return !s.startsWith('http')
          || s.indexOf('google.com/search') !== -1
          || s.indexOf('accounts.google') !== -1
          || s.indexOf('webcache') !== -1
          || s.indexOf('support.google') !== -1
          || s.indexOf('policies.google') !== -1;
      };
      const selectors = [
        '#search a h3',
        '#rso a h3',
        'div[data-snhf] a h3',
        'a h3',
        '#rso a[jsname][href]',
        'a[href^="http"]'
      ];
      const seen = new Set();
      for (const sel of selectors) {
        for (const node of document.querySelectorAll(sel)) {
          const a = node.tagName === 'A' ? node : node.closest('a');
          if (!a || seen.has(a)) continue;
          seen.add(a);
          const href = unwrap(a.href);
          if (!bad(href) && href.toLowerCase().indexOf('google.com') === -1) return href;
        }
      }
      return '';
    })()
    """

    private func extractGoogleFirstHref(browser: String) -> String {
        let js = Self.googleFirstResultJS
        let script: String
        if browser == "safari" {
            script = "tell application id \"com.apple.Safari\" to do JavaScript \(asLiteral(js)) in current tab of window 1"
        } else {
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then return ""
              execute (active tab of front window) javascript \(asLiteral(js))
            end tell
            """
        }
        var href = ""
        for _ in 0..<3 {
            let found = runAppleScript(script)
            href = found.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if found.ok, href.hasPrefix("http") { return href }
            Thread.sleep(forTimeInterval: 0.3)
        }
        return href
    }

    private func firstResultSearchQuery(_ arguments: [String: Any], stored: String?) -> String? {
        let raw = locationQuery(arguments)
        let lower = raw.lowercased()
        if raw.isEmpty
            || lower.contains("first result")
            || lower.contains("top result")
            || lower.contains("first search")
            || lower.contains("result link")
            || lower.contains("first video")
            || lower.contains("first item")
            || lower.contains("first one")
            || lower.contains("play the first")
        {
            let kept = (stored ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return kept.isEmpty ? nil : kept
        }
        return raw
    }

    private func isSearchResultsURL(_ url: String) -> Bool {
        let lower = url.lowercased()
        return lower.contains("google.com/search")
            || lower.contains("bing.com/search")
            || lower.contains("duckduckgo.com/?q")
            || lower.contains("search.yahoo.com")
    }

    private func isEmptyBrowserURL(_ url: String) -> Bool {
        let lower = url.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        if lower.isEmpty { return true }
        return lower.hasPrefix("favorites:")
            || lower.hasPrefix("topsites:")
            || lower.hasPrefix("about:")
            || lower.contains("://newtab")
            || lower.hasPrefix("chrome://new")
            || lower.hasPrefix("safari-resource:")
            || lower == "https://www.google.com/"
            || lower == "https://google.com/"
    }

    private func isMediaListingURL(_ url: String) -> Bool {
        let lower = url.lowercased()
        if lower.contains("youtube.com/results") || lower.contains("youtube.com/feed") { return true }
        if lower.contains("vimeo.com/search") || lower.contains("dailymotion.com/search") { return true }
        if let parsed = URL(string: lower), let host = parsed.host?.lowercased() {
            let path = parsed.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            if (host == "youtube.com" || host == "www.youtube.com" || host == "m.youtube.com") && path.isEmpty {
                return true
            }
            if host == "vimeo.com" && path.isEmpty { return true }
        }
        return false
    }

    private func looksLikeContentItemURL(_ url: String) -> Bool {
        let lower = url.lowercased()
        if isSearchResultsURL(lower) || isMediaListingURL(lower) { return false }
        return lower.contains("/watch")
            || lower.contains("?v=")
            || lower.contains("&v=")
            || lower.contains("/status/")
            || lower.contains("/shorts/")
            || lower.contains("/reel")
            || lower.contains("/dp/")
            || lower.range(of: #"vimeo\.com/\d+"#, options: .regularExpression) != nil
            || lower.contains("dailymotion.com/video/")
    }

    private func queryLooksLikeMedia(_ query: String) -> Bool {
        let lower = query.lowercased()
        if lower.isEmpty { return false }
        return lower.contains("first video")
            || lower.contains("play the first")
            || lower == "first"
            || lower == "second"
            || lower == "third"
            || lower.contains("video")
            || lower.hasPrefix("play ")
    }

    private func mediaOrdinal(from query: String) -> Int {
        let lower = query.lowercased()
        if lower.contains("second") || lower.contains("2nd") { return 2 }
        if lower.contains("third") || lower.contains("3rd") { return 3 }
        if lower.contains("fourth") || lower.contains("4th") { return 4 }
        if lower.contains("fifth") || lower.contains("5th") { return 5 }
        return 1
    }

    private func mediaNeedle(from query: String) -> String {
        var raw = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let lower = raw.lowercased()
        if raw.isEmpty
            || lower == "first"
            || lower.contains("first video")
            || lower.contains("first item")
            || lower.contains("first one")
            || lower.contains("play the first")
            || lower == "second"
            || lower == "third"
        {
            return ""
        }
        let stripped = raw.replacingOccurrences(
            of: #"(?i)^(play|watch|open)\s+(the\s+)?(video|clip|movie|file)?\s*"#,
            with: "",
            options: .regularExpression
        )
        return stripped.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let pageFirstContentJS = """
    (function() {
      function unwrap(href) {
        try {
          var u = new URL(href, location.href);
          var uddg = u.searchParams.get('uddg');
          if (uddg) return decodeURIComponent(uddg);
          return u.href;
        } catch (e) { return href || ''; }
      }
      function skip(href, text) {
        var h = (href || '').toLowerCase();
        var t = (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (!h || h.indexOf('javascript:') === 0 || h === '#' || h.indexOf('mailto:') === 0) return true;
        if (/accounts\\.google|consent\\.|support\\.google|policies\\.google/.test(h)) return true;
        if (/^(sign in|log in|privacy|terms|cookies?|help|about|contact|home|menu|shorts|subscriptions|library)$/.test(t)) return true;
        return false;
      }
      var sels = [
        'ytd-rich-item-renderer a#video-title-link',
        'ytd-video-renderer a#video-title',
        'a[href*="/watch"]',
        'a[href*="/status/"]',
        '[role="listitem"] a[href]',
        '[role="article"] a[href]',
        'main a[href]',
        '#content a[href]',
        '.search-result a[href]',
        '.result a[href]',
        '[data-testid="result"] a[href]',
        'a[href^="http"]',
        'a[href^="/"]'
      ];
      var start = location.href.split('#')[0];
      for (var s = 0; s < sels.length; s++) {
        var nodes = document.querySelectorAll(sels[s]);
        for (var i = 0; i < nodes.length; i++) {
          var node = nodes[i];
          var a = node.tagName === 'A' ? node : node.closest('a');
          if (!a) continue;
          var href = unwrap(a.href || a.getAttribute('href') || '');
          var text = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim();
          if (skip(href, text)) continue;
          if ((href.split('#')[0] === start) && href.toLowerCase().indexOf('/watch') === -1) continue;
          try { a.scrollIntoView({block: 'center'}); } catch (e) {}
          a.click();
          return href || text || 'clicked';
        }
      }
      return '';
    })()
    """

    private func runPageJS(_ js: String, browser: String) -> String {
        let script: String
        if browser == "safari" {
            script = "tell application id \"com.apple.Safari\" to do JavaScript \(asLiteral(js)) in current tab of window 1"
        } else {
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then return ""
              execute (active tab of front window) javascript \(asLiteral(js))
            end tell
            """
        }
        var href = ""
        for _ in 0..<4 {
            let found = runAppleScript(script)
            href = found.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if found.ok, !href.isEmpty, href != "missing value" { return href }
            Thread.sleep(forTimeInterval: 0.35)
        }
        return href
    }

    private func clickFirstOnPageItem(browser: String) -> String {
        let href = runPageJS(Self.pageFirstContentJS, browser: browser)
        Thread.sleep(forTimeInterval: 1.0)
        return href
    }

    private static let pageMediaCandidatesJS = """
    (function() {
      function unwrap(href) {
        try {
          var u = new URL(href, location.href);
          var uddg = u.searchParams.get('uddg');
          if (uddg) return decodeURIComponent(uddg);
          if (u.hostname.indexOf('google.') !== -1 && (u.pathname === '/url' || u.pathname.indexOf('/url') === 0)) {
            return u.searchParams.get('q') || u.searchParams.get('url') || u.href;
          }
          return u.href;
        } catch (e) { return href || ''; }
      }
      function isMedia(href) {
        var h = (href || '').toLowerCase();
        return /\\/watch(\\?|\\/|$)|[?&]v=|\\/shorts\\/|\\/reel|vimeo\\.com\\/\\d+|dailymotion\\.com\\/video\\//.test(h);
      }
      var sels = [
        'ytd-rich-item-renderer a#video-title-link',
        'ytd-video-renderer a#video-title',
        'ytd-compact-video-renderer a.yt-simple-endpoint',
        'a#thumbnail[href*="/watch"]',
        'a[href*="/watch"]',
        'a[href*="/shorts/"]',
        'a[href*="vimeo.com/"]',
        'a[href*="dailymotion.com/video"]',
        'a[href*="/videos/"]'
      ];
      var seen = {};
      var out = [];
      for (var s = 0; s < sels.length; s++) {
        var nodes = document.querySelectorAll(sels[s]);
        for (var i = 0; i < nodes.length; i++) {
          var node = nodes[i];
          var a = node.tagName === 'A' ? node : node.closest('a');
          if (!a) continue;
          var href = unwrap(a.href || a.getAttribute('href') || '');
          if (!isMedia(href) || seen[href]) continue;
          seen[href] = 1;
          var title = (a.getAttribute('title') || a.getAttribute('aria-label') || a.innerText || '').replace(/\\s+/g, ' ').trim();
          out.push(href + '\\t' + title);
          if (out.length >= 24) return out.join('\\n');
        }
      }
      return out.join('\\n');
    })()
    """

    private static let pageEnsurePlayingJS = """
    (function() {
      function clickLabeled(re) {
        var nodes = document.querySelectorAll('button, [role="button"], .ytp-large-play-button, .ytp-play-button');
        for (var i = 0; i < nodes.length; i++) {
          var t = ((nodes[i].getAttribute('aria-label') || nodes[i].innerText || nodes[i].title || '') + '').toLowerCase();
          if (re.test(t)) { nodes[i].click(); return true; }
        }
        return false;
      }
      clickLabeled(/accept all|i agree|agree|accept cookies|got it/);
      var v = document.querySelector('video.html5-main-video, video');
      if (v) {
        try { v.muted = false; } catch (e) {}
        try { var p = v.play(); if (p && p.catch) p.catch(function(){}); } catch (e) {}
        if (!v.paused) return 'playing';
        clickLabeled(/^(play$|play video|play \\()/);
        try { v.play(); } catch (e) {}
        return v.paused ? 'paused' : 'playing';
      }
      clickLabeled(/^(play$|play video)/);
      return 'none';
    })()
    """

    private static let pagePlayerStateJS = """
    (function() {
      var v = document.querySelector('video.html5-main-video, video');
      if (!v) return 'none';
      if (!v.paused && v.readyState >= 2) return 'playing';
      if (!v.paused) return 'playing';
      return 'paused';
    })()
    """

    private func browserNow(_ browser: String) -> (url: String, title: String) {
        browser == "chrome" ? chromeNow() : safariNow()
    }

    private func setBrowserURL(_ url: String, browser: String) {
        let script: String
        if browser == "chrome" {
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then
                make new window
              end if
              set URL of active tab of front window to \(asLiteral(url))
            end tell
            """
        } else {
            script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then
                make new document
              end if
              set URL of current tab of window 1 to \(asLiteral(url))
            end tell
            """
        }
        _ = runAppleScript(script)
    }

    private func browserHTML(_ browser: String) -> String {
        if browser == "safari" {
            let script = """
            tell application id "com.apple.Safari"
              if (count of windows) is 0 then return ""
              try
                return source of front document
              end try
              try
                return source of document 1
              end try
              return ""
            end tell
            """
            let ran = runAppleScript(script, timeout: 14)
            if ran.ok, ran.text.count > 200 { return ran.text }
        }
        let js = "(function(){try{return document.documentElement.outerHTML.slice(0,1800000)}catch(e){return ''}})()"
        return runPageJS(js, browser: browser)
    }

    private func extractMediaCandidates(from html: String) -> [(href: String, title: String)] {
        var items: [(href: String, title: String)] = []
        var seen = Set<String>()
        func add(_ href: String, title: String = "") {
            let clean = href.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !clean.isEmpty, seen.insert(clean).inserted else { return }
            items.append((clean, title))
        }
        let ns = html as NSString
        let limit = NSRange(location: 0, length: min(ns.length, 1_800_000))
        if let re = try? NSRegularExpression(pattern: #""videoId":"([\w-]{11})""#) {
            for match in re.matches(in: html, range: limit) {
                let id = ns.substring(with: match.range(at: 1))
                add("https://www.youtube.com/watch?v=\(id)", title: titleNearVideoId(html, id: id))
            }
        }
        if let re = try? NSRegularExpression(pattern: #"(?:https?://(?:www\.)?youtube\.com)?/watch\?v=([\w-]{6,11})"#) {
            for match in re.matches(in: html, range: limit) {
                let id = ns.substring(with: match.range(at: 1))
                add("https://www.youtube.com/watch?v=\(id)", title: titleNearVideoId(html, id: id))
            }
        }
        if let re = try? NSRegularExpression(pattern: #"https?://(?:www\.)?youtu\.be/([\w-]{6,11})"#) {
            for match in re.matches(in: html, range: limit) {
                let id = ns.substring(with: match.range(at: 1))
                add("https://www.youtube.com/watch?v=\(id)")
            }
        }
        if let re = try? NSRegularExpression(pattern: #"https?://(?:www\.)?vimeo\.com/(\d{5,})"#) {
            for match in re.matches(in: html, range: limit) {
                add(ns.substring(with: match.range))
            }
        }
        if let re = try? NSRegularExpression(pattern: #"https?://(?:www\.)?dailymotion\.com/video/([\w]+)"#) {
            for match in re.matches(in: html, range: limit) {
                add(ns.substring(with: match.range))
            }
        }
        return items
    }

    private func titleNearVideoId(_ html: String, id: String) -> String {
        let needle = "\"videoId\":\"\(id)\""
        guard let found = html.range(of: needle) else { return "" }
        let start = html.index(found.lowerBound, offsetBy: -240, limitedBy: html.startIndex) ?? html.startIndex
        let end = html.index(found.upperBound, offsetBy: 520, limitedBy: html.endIndex) ?? html.endIndex
        let window = String(html[start..<end])
        guard let re = try? NSRegularExpression(pattern: #""text":"([^"\\]{6,140})""#) else { return "" }
        let ns = window as NSString
        var best = ""
        for match in re.matches(in: window, range: NSRange(location: 0, length: ns.length)) {
            let text = ns.substring(with: match.range(at: 1))
            if text.range(of: #"^\d+:\d+"#, options: .regularExpression) != nil { continue }
            let lower = text.lowercased()
            if ["views", "subscribers", "subscribe", "mix", "shorts"].contains(lower) { continue }
            if text.count > best.count { best = text }
        }
        return best
    }

    private func parseMediaCandidateLines(_ raw: String) -> [(href: String, title: String)] {
        var items: [(href: String, title: String)] = []
        var seen = Set<String>()
        for line in raw.split(separator: "\n") {
            let parts = line.split(separator: "\t", maxSplits: 1, omittingEmptySubsequences: false)
            let href = String(parts[0]).trimmingCharacters(in: .whitespacesAndNewlines)
            let title = parts.count > 1 ? String(parts[1]).trimmingCharacters(in: .whitespacesAndNewlines) : ""
            guard href.hasPrefix("http"), seen.insert(href).inserted else { continue }
            items.append((href, title))
        }
        return items
    }

    private func pickMediaCandidate(
        _ items: [(href: String, title: String)],
        needle: String,
        ordinal: Int
    ) -> (href: String, title: String)? {
        guard !items.isEmpty else { return nil }
        let want = needle.lowercased()
        if !want.isEmpty {
            if let exact = items.first(where: { $0.title.lowercased().contains(want) || $0.href.lowercased().contains(want) }) {
                return exact
            }
            let tokens = want.split(separator: " ").map { String($0) }.filter { $0.count > 2 }
            if !tokens.isEmpty {
                if let fuzzy = items.first(where: { item in
                    let blob = (item.title + " " + item.href).lowercased()
                    return tokens.filter { blob.contains($0) }.count >= max(1, tokens.count / 2)
                }) {
                    return fuzzy
                }
            }
        }
        let index = max(1, ordinal) - 1
        if items.indices.contains(index) { return items[index] }
        return items.first
    }

    private func waitForBrowserCondition(browser: String, attempts: Int, interval: TimeInterval, _ test: (String, String) -> Bool) -> (url: String, title: String) {
        var now = browserNow(browser)
        if test(now.url, now.title) { return now }
        for _ in 0..<attempts {
            Thread.sleep(forTimeInterval: interval)
            now = browserNow(browser)
            if test(now.url, now.title) { return now }
        }
        return now
    }

    private func collectMediaCandidates(browser: String) -> [(href: String, title: String)] {
        var items: [(href: String, title: String)] = []
        for _ in 0..<12 {
            let htmlItems = extractMediaCandidates(from: browserHTML(browser))
            if !htmlItems.isEmpty { return htmlItems }
            let jsItems = parseMediaCandidateLines(runPageJS(Self.pageMediaCandidatesJS, browser: browser))
            if !jsItems.isEmpty { return jsItems }
            items = htmlItems
            Thread.sleep(forTimeInterval: 0.6)
        }
        return items
    }

    private func dismissBrowserConsent(browser: String) {
        _ = runPageJS(Self.pageEnsurePlayingJS, browser: browser)
        _ = axClickLabeledButton(browser: browser, labels: ["accept all", "accept", "i agree", "agree", "got it"])
    }

    private func axClickLabeledButton(browser: String, labels: [String]) -> Bool {
        guard AXIsProcessTrusted() else { return false }
        let apps = browser == "chrome"
            ? NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome")
            : safariContentApps()
        for running in apps.prefix(4) {
            let appEl = AXUIElementCreateApplication(running.processIdentifier)
            var walked: [WalkItem] = []
            var walkCount = 0
            let windows = axElements(appEl, kAXWindowsAttribute)
            for root in windows.prefix(2) {
                walk(root, depth: 0, deadline: Date().addingTimeInterval(1.1), maxWalk: 360, query: "", into: &walked, walkedCount: &walkCount)
            }
            for item in walked where item.enabled {
                let blob = (item.title + " " + item.value).lowercased()
                let role = item.role.lowercased()
                guard role.contains("button") || item.actions.contains("AXPress") else { continue }
                if labels.contains(where: { blob == $0 || blob.hasPrefix($0) }) {
                    if AXUIElementPerformAction(item.element, kAXPressAction as CFString) == .success {
                        return true
                    }
                    if let point = hidCenter(of: item.element) {
                        click(point)
                        return true
                    }
                }
            }
        }
        return false
    }

    private func axPlayerIsPlaying(browser: String) -> Bool {
        guard AXIsProcessTrusted() else { return false }
        let apps = browser == "chrome"
            ? NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome")
            : safariContentApps()
        for running in apps.prefix(4) {
            let appEl = AXUIElementCreateApplication(running.processIdentifier)
            var walked: [WalkItem] = []
            var walkCount = 0
            let windows = axElements(appEl, kAXWindowsAttribute)
            for root in windows.prefix(2) {
                walk(root, depth: 0, deadline: Date().addingTimeInterval(1.0), maxWalk: 320, query: "", into: &walked, walkedCount: &walkCount)
            }
            for item in walked {
                let blob = (item.title + " " + item.value).lowercased()
                if blob == "pause" || blob.hasPrefix("pause") || blob.contains("pause video") {
                    return true
                }
            }
        }
        return false
    }

    private func axClickMediaCard(browser: String, needle: String, ordinal: Int) -> Bool {
        guard AXIsProcessTrusted() else { return false }
        let skip = [
            "home", "shorts", "subscriptions", "you", "history", "library", "sign in",
            "search", "skip navigation", "images", "videos", "maps", "news", "shopping",
            "more", "tools", "settings", "privacy", "terms", "help", "about"
        ]
        let apps = browser == "chrome"
            ? NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome")
            : safariContentApps()
        var scored: [(item: WalkItem, area: CGFloat)] = []
        for running in apps.prefix(4) {
            let appEl = AXUIElementCreateApplication(running.processIdentifier)
            var walked: [WalkItem] = []
            var walkCount = 0
            let windows = axElements(appEl, kAXWindowsAttribute)
            for root in windows.prefix(3) {
                walk(root, depth: 0, deadline: Date().addingTimeInterval(1.6), maxWalk: 520, query: needle, into: &walked, walkedCount: &walkCount)
            }
            for item in walked where item.enabled && item.role == "AXLink" {
                let blob = item.title.lowercased()
                if blob.isEmpty || skip.contains(where: { blob == $0 || blob.hasPrefix($0 + " ") }) { continue }
                if blob.count < 6 { continue }
                let size = axSize(item.element) ?? .zero
                let area = size.width * size.height
                if size.width < 40 || size.height < 12 { continue }
                scored.append((item, area))
            }
        }
        scored.sort { $0.area > $1.area }
        let want = needle.lowercased()
        var picks = scored.map(\.item)
        if !want.isEmpty {
            let matched = picks.filter { $0.title.lowercased().contains(want) }
            if !matched.isEmpty { picks = matched }
        }
        let index = max(1, ordinal) - 1
        guard picks.indices.contains(index) || picks.first != nil else { return false }
        let target = picks.indices.contains(index) ? picks[index] : picks[0]
        if itemSupportsPress(target) {
            if AXUIElementPerformAction(target.element, kAXPressAction as CFString) == .success {
                return true
            }
        }
        if let point = hidCenter(of: target.element) {
            click(point)
            return true
        }
        return false
    }

    private func clickBrowserPlayerRegion(browser: String) {
        let bundle = browser == "chrome" ? "com.google.Chrome" : "com.apple.Safari"
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first else { return }
        app.activate(options: [.activateAllWindows])
        Thread.sleep(forTimeInterval: 0.12)
        let appEl = AXUIElementCreateApplication(app.processIdentifier)
        let windows = axElements(appEl, kAXWindowsAttribute)
        guard let window = windows.first,
              let origin = axPoint(window),
              let size = axSize(window),
              size.width > 200, size.height > 200
        else { return }
        let cocoa = CGPoint(
            x: origin.x + size.width * 0.34,
            y: origin.y + 132 + min(180, size.height * 0.20)
        )
        click(hidFromCocoa(cocoa))
    }

    private func ensureHTML5VideoPlaying(browser: String) -> String {
        for _ in 0..<8 {
            let js = runPageJS(Self.pagePlayerStateJS, browser: browser)
            if js == "playing" { return "playing" }
            if axPlayerIsPlaying(browser: browser) { return "playing" }
            _ = runPageJS(Self.pageEnsurePlayingJS, browser: browser)
            clickBrowserPlayerRegion(browser: browser)
            Thread.sleep(forTimeInterval: 0.12)
            _ = axClickLabeledButton(browser: browser, labels: ["play", "play video", "play now", "skip", "skip ad"])
            _ = postHotkey("k")
            Thread.sleep(forTimeInterval: 0.4)
            if axPlayerIsPlaying(browser: browser) { return "playing" }
            if runPageJS(Self.pagePlayerStateJS, browser: browser) == "playing" { return "playing" }
            _ = postHotkey("space")
            Thread.sleep(forTimeInterval: 0.45)
            if axPlayerIsPlaying(browser: browser) { return "playing" }
            let again = runPageJS(Self.pagePlayerStateJS, browser: browser)
            if again == "playing" { return "playing" }
        }
        if axPlayerIsPlaying(browser: browser) { return "playing" }
        let last = runPageJS(Self.pagePlayerStateJS, browser: browser)
        return last.isEmpty ? "paused" : last
    }

    private func playMediaInBrowser(browser: String, arguments: [String: Any], requestId: String) -> [String: Any] {
        let query = locationQuery(arguments)
        let needle = mediaNeedle(from: query)
        let ordinal = mediaOrdinal(from: query)
        if browser == "safari" {
            _ = runAppleScript("tell application id \"com.apple.Safari\" to activate")
        } else {
            _ = runAppleScript("tell application id \"com.google.Chrome\" to activate")
        }
        var now = browserNow(browser)
        if isEmptyBrowserURL(now.url) {
            setBrowserURL("https://www.youtube.com/", browser: browser)
            now = waitForBrowserCondition(browser: browser, attempts: 18, interval: 0.4) { url, _ in
                !self.isEmptyBrowserURL(url)
            }
        }
        dismissBrowserConsent(browser: browser)
        now = browserNow(browser)
        let currentTitle = now.title.lowercased()
        let currentMatchesNeedle = needle.isEmpty
            || currentTitle.contains(needle.lowercased())
            || now.url.lowercased().contains(needle.lowercased())
        if looksLikeContentItemURL(now.url) && currentMatchesNeedle {
            Thread.sleep(forTimeInterval: 0.8)
            let player = ensureHTML5VideoPlaying(browser: browser)
            now = browserNow(browser)
            return mediaPlayReceipt(
                browser: browser,
                requestId: requestId,
                url: now.url,
                title: now.title,
                player: player,
                method: "player_on_item"
            )
        }
        var candidates = collectMediaCandidates(browser: browser)
        if candidates.isEmpty {
            _ = axClickMediaCard(browser: browser, needle: needle, ordinal: ordinal)
            Thread.sleep(forTimeInterval: 1.2)
            now = browserNow(browser)
            if looksLikeContentItemURL(now.url) {
                let player = ensureHTML5VideoPlaying(browser: browser)
                now = browserNow(browser)
                return mediaPlayReceipt(
                    browser: browser,
                    requestId: requestId,
                    url: now.url,
                    title: now.title,
                    player: player,
                    method: "ax_card"
                )
            }
            candidates = collectMediaCandidates(browser: browser)
        }
        if let picked = pickMediaCandidate(candidates, needle: needle, ordinal: ordinal) {
            setBrowserURL(picked.href, browser: browser)
            now = waitForBrowserCondition(browser: browser, attempts: 16, interval: 0.4) { url, _ in
                self.looksLikeContentItemURL(url)
            }
            Thread.sleep(forTimeInterval: 1.1)
            let player = ensureHTML5VideoPlaying(browser: browser)
            now = browserNow(browser)
            return mediaPlayReceipt(
                browser: browser,
                requestId: requestId,
                url: now.url,
                title: now.title.isEmpty ? picked.title : now.title,
                player: player,
                method: "open_media_url",
                clicked: picked.href
            )
        }
        now = browserNow(browser)
        return mediaPlayReceipt(
            browser: browser,
            requestId: requestId,
            url: now.url,
            title: now.title,
            player: "none",
            method: "failed"
        )
    }

    private func mediaPlayReceipt(
        browser: String,
        requestId: String,
        url: String,
        title: String,
        player: String,
        method: String,
        clicked: String = ""
    ) -> [String: Any] {
        let playing = player == "playing"
        let onItem = looksLikeContentItemURL(url)
        let verified = playing
        let app = browser == "chrome" ? "Chrome" : "Safari"
        let spoken: String
        if playing {
            spoken = "Playing \(title.isEmpty ? "the video" : title)."
        } else if onItem {
            spoken = "Opened the video but it is not playing yet."
        } else {
            spoken = "I couldn't play that video."
        }
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "must_continue": !verified,
                "adapter": browser,
                "app": app,
                "action": "play",
                "url": url,
                "title": title,
                "clicked": clicked,
                "player_state": playing ? "playing" : (onItem ? "paused" : player),
                "method": method,
                "goal_complete": verified,
                "suggested_fallbacks": verified ? [] : ["ui_action"],
                "spoken": spoken,
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func safariOpenFirstResult(requestId: String, query: String = "") -> [String: Any] {
        if let needle = firstResultSearchQuery(["query": query], stored: lastSafariQuery) {
            lastSafariQuery = needle
        }
        let start = safariNow()
        if !isSearchResultsURL(start.url) {
            Thread.sleep(forTimeInterval: 1.2)
            var href = clickFirstOnPageItem(browser: "safari")
            if href.isEmpty {
                _ = safariPressFirstLink()
                Thread.sleep(forTimeInterval: 0.8)
            }
            let status = safariNow()
            let verified = looksLikeContentItemURL(status.url)
            return ok(
                [
                    "ok": verified,
                    "executed": true,
                    "verified": verified,
                    "must_continue": !verified,
                    "adapter": "safari",
                    "app": "Safari",
                    "action": "navigate",
                    "url": status.url,
                    "title": status.title,
                    "clicked": href,
                    "method": verified ? "on_page_item" : "failed",
                    "suggested_fallbacks": verified ? [] : ["screen_look", "ui_action"],
                    "spoken": verified
                        ? "Opened \(status.title.isEmpty ? status.url : status.title)."
                        : "I couldn't open the first item on this page.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: verified
            )
        }
        var href = extractGoogleFirstHref(browser: "safari")
        var stillSearch = isSearchResultsURL(safariNow().url)
        if href.hasPrefix("http"), stillSearch {
            _ = runAppleScript(
                "tell application id \"com.apple.Safari\" to set URL of current tab of window 1 to \(asLiteral(href))"
            )
            Thread.sleep(forTimeInterval: 1.0)
            stillSearch = isSearchResultsURL(safariNow().url)
        }
        if stillSearch, let needle = lastSafariQuery, let discovered = discoverFirstWebResult(needle) {
            href = discovered
            _ = runAppleScript(
                "tell application id \"com.apple.Safari\" to set URL of current tab of window 1 to \(asLiteral(discovered))"
            )
            Thread.sleep(forTimeInterval: 1.2)
            stillSearch = isSearchResultsURL(safariNow().url)
        }
        if stillSearch {
            if safariPressFirstLink() {
                Thread.sleep(forTimeInterval: 0.8)
                stillSearch = isSearchResultsURL(safariNow().url)
            }
        }
        if stillSearch {
            if safariTabToFirstResult() {
                Thread.sleep(forTimeInterval: 0.8)
                stillSearch = isSearchResultsURL(safariNow().url)
            }
        }
        let status = safariNow()
        let verified = !isSearchResultsURL(status.url) && !status.url.isEmpty
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "must_continue": !verified,
                "adapter": "safari",
                "app": "Safari",
                "action": "navigate",
                "url": status.url,
                "title": status.title,
                "clicked": href,
                "method": verified ? "discovered_url" : "failed",
                "suggested_fallbacks": verified ? [] : ["screen_look", "ui_action"],
                "spoken": verified
                    ? "Opened \(status.title.isEmpty ? status.url : status.title)."
                    : "Safari is still on the search page. Look at the window and click the first result.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func discoverFirstWebResult(_ query: String) -> String? {
        let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
        guard let url = URL(string: "https://html.duckduckgo.com/html/?q=\(encoded)") else { return nil }
        var request = URLRequest(url: url, timeoutInterval: 5)
        request.setValue("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", forHTTPHeaderField: "User-Agent")
        let box = NativeDataBox()
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, _, _ in
            box.data = data
            done.signal()
        }.resume()
        if done.wait(timeout: .now() + 6) == .timedOut { return nil }
        guard let data = box.data, let html = String(data: data, encoding: .utf8) else { return nil }
        let pattern = #"uddg=([^&"]+)|href="(https?://[^"]+)""#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return nil }
        let ns = html as NSString
        let matches = regex.matches(in: html, range: NSRange(location: 0, length: ns.length))
        for match in matches {
            var raw = ""
            if match.range(at: 1).location != NSNotFound {
                raw = ns.substring(with: match.range(at: 1))
            } else if match.range(at: 2).location != NSNotFound {
                raw = ns.substring(with: match.range(at: 2))
            }
            raw = raw.removingPercentEncoding ?? raw
            let lower = raw.lowercased()
            if !lower.hasPrefix("http") { continue }
            if lower.contains("duckduckgo.com") || lower.contains("google.com") { continue }
            if lower.contains("bing.com") || lower.contains("yahoo.com") { continue }
            if lower.contains("/redirect") { continue }
            return raw
        }
        return nil
    }

    private func safariContentApps() -> [NSRunningApplication] {
        var apps: [NSRunningApplication] = []
        apps.append(contentsOf: NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari"))
        for app in NSWorkspace.shared.runningApplications {
            let name = (app.localizedName ?? "").lowercased()
            let bundle = (app.bundleIdentifier ?? "").lowercased()
            if name.contains("web content") || bundle.contains("webkit") {
                apps.append(app)
            }
        }
        var seen = Set<pid_t>()
        return apps.filter { seen.insert($0.processIdentifier).inserted }
    }

    private func safariPressFirstLink() -> Bool {
        guard AXIsProcessTrusted() else { return false }
        let skip = ["images", "videos", "maps", "news", "shopping", "more", "tools", "settings", "sign in"]
        for running in safariContentApps() {
            let appEl = AXUIElementCreateApplication(running.processIdentifier)
            var walked: [WalkItem] = []
            var walkCount = 0
            let windows = axElements(appEl, kAXWindowsAttribute)
            let window = axElement(appEl, kAXFocusedWindowAttribute) ?? windows.first
            let roots = (windows.isEmpty ? [appEl] : windows)
            for root in roots.prefix(3) {
                walk(root, depth: 0, deadline: Date().addingTimeInterval(1.4), maxWalk: 420, query: "", into: &walked, walkedCount: &walkCount)
            }
            if let window, windows.isEmpty {
                walk(window, depth: 0, deadline: Date().addingTimeInterval(1.2), maxWalk: 300, query: "", into: &walked, walkedCount: &walkCount)
            }
            let links = walked.filter { $0.role == "AXLink" && $0.enabled && !$0.title.isEmpty }
                .filter { item in
                    let blob = item.title.lowercased()
                    return !skip.contains(where: { blob == $0 || blob.hasPrefix($0 + " ") })
                        && !blob.contains("google.com/search")
                        && blob != "about this result"
                }
            if let first = links.first {
                if itemSupportsPress(first) {
                    if AXUIElementPerformAction(first.element, kAXPressAction as CFString) == .success {
                        return true
                    }
                }
                _ = AXUIElementSetAttributeValue(first.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
                if postHotkey("return") { return true }
            }
        }
        return false
    }

    private func itemSupportsPress(_ item: WalkItem) -> Bool {
        item.actions.contains("AXPress") || item.actions.contains(kAXPressAction as String)
    }

    private func safariTabToFirstResult() -> Bool {
        _ = runAppleScript("tell application id \"com.apple.Safari\" to activate")
        Thread.sleep(forTimeInterval: 0.2)
        for _ in 0..<14 {
            _ = postHotkey("tab")
            Thread.sleep(forTimeInterval: 0.05)
            guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari").first else { continue }
            let focused = axElement(AXUIElementCreateApplication(app.processIdentifier), kAXFocusedUIElementAttribute)
            let role = focused.flatMap { stringValue($0, kAXRoleAttribute) } ?? ""
            let title = focused.flatMap { stringValue($0, kAXTitleAttribute) } ?? ""
            if role == "AXLink" || title.lowercased().contains("http") {
                return postHotkey("return")
            }
        }
        return false
    }

    private func calculatorAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        _ = runAppleScript("tell application id \"com.apple.calculator\" to activate")
        Thread.sleep(forTimeInterval: 0.2)
        let action = (string(arguments, "action") ?? "status").lowercased()
        if action == "status" || action == "read" {
            let display = calculatorDisplay()
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": !display.isEmpty,
                    "adapter": "keyboard",
                    "app": "Calculator",
                    "action": "status",
                    "method": "keyboard",
                    "display": display,
                    "spoken": display.isEmpty ? "Calculator is open." : "Calculator shows \(display).",
                ],
                command: "app_action",
                requestId: requestId
            )
        }
        let raw = string(arguments, "value")
            ?? string(arguments, "query")
            ?? string(arguments, "text")
            ?? ""
        var keys = raw.replacingOccurrences(of: "×", with: "*")
            .replacingOccurrences(of: "x", with: "*", options: .caseInsensitive)
            .replacingOccurrences(of: "÷", with: "/")
            .replacingOccurrences(of: "times", with: "*")
            .filter { $0.isNumber || "+-*/=.".contains($0) }
        if keys.isEmpty {
            return fail("missing_text", "What should I calculate?", command: "app_action", requestId: requestId)
        }
        if !keys.contains("=") { keys += "=" }
        postHotkey("escape")
        Thread.sleep(forTimeInterval: 0.05)
        let typed = typeCalculatorKeys(keys)
        _ = postHotkey("return")
        Thread.sleep(forTimeInterval: 0.45)
        let display = calculatorDisplay()
        let verified = !display.isEmpty && display.contains(where: { $0.isNumber })
        return ok(
            [
                "ok": typed && verified,
                "executed": typed,
                "verified": verified,
                "adapter": "keyboard",
                "app": "Calculator",
                "action": action,
                "method": "keyboard",
                "keys": keys,
                "display": display,
                "spoken": verified ? "Calculator shows \(display)." : "I sent keys to Calculator but could not read the display.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: typed
        )
    }

    private func typeCalculatorKeys(_ keys: String) -> Bool {
        var ok = true
        for ch in keys {
            let spec: String
            switch ch {
            case "*": spec = "*"
            case "+": spec = "+"
            case "-": spec = "-"
            case "/": spec = "/"
            case "=":
                ok = postHotkey("return") && ok
                continue
            case ".": spec = "."
            default: spec = String(ch)
            }
            ok = postHotkey(spec) && ok
            Thread.sleep(forTimeInterval: 0.04)
        }
        return ok
    }

    private func calculatorDisplay() -> String {
        guard AXIsProcessTrusted() else { return "" }
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.calculator").first
        guard let running else { return "" }
        let appEl = AXUIElementCreateApplication(running.processIdentifier)
        var walked: [WalkItem] = []
        var walkCount = 0
        if let window = axElement(appEl, kAXFocusedWindowAttribute) ?? axElements(appEl, kAXWindowsAttribute).first {
            walk(window, depth: 0, deadline: Date().addingTimeInterval(0.8), maxWalk: 80, query: "", into: &walked, walkedCount: &walkCount)
        }
        for item in walked {
            let role = item.role.lowercased()
            if role.contains("static") || role.contains("text") || role.contains("value") {
                let value = stringValue(item.element, kAXValueAttribute) ?? item.title
                if value.contains(where: { $0.isNumber }) { return value }
            }
        }
        let script = """
        tell application "System Events"
          tell process "Calculator"
            if (count of windows) is 0 then return ""
            try
              return value of static text 1 of window 1
            end try
            try
              return value of static text 1 of group 1 of window 1
            end try
            return ""
          end tell
        end tell
        """
        return runAppleScript(script).text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func safariStatus(requestId: String, spokenPrefix: String?) -> [String: Any] {
        let now = safariNow()
        return ok(
            [
                "ok": true,
                "executed": true,
                "verified": !now.url.isEmpty,
                "adapter": "safari",
                "app": "Safari",
                "action": "status",
                "url": now.url,
                "title": now.title,
                "spoken": spokenPrefix ?? (now.title.isEmpty ? now.url : now.title),
            ],
            command: "app_action",
            requestId: requestId
        )
    }

    private func notesAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "status").lowercased()
        if action != "read", action != "status" {
            _ = runAppleScript("tell application id \"com.apple.Notes\" to activate")
        }
        let body = string(arguments, "value") ?? string(arguments, "text") ?? string(arguments, "query") ?? ""
        switch action {
        case "create":
            if body.isEmpty {
                return fail("missing_text", "What should the note say?", command: "app_action", requestId: requestId)
            }
            let name = String(body.prefix(40))
            let script = """
            tell application id "com.apple.Notes"
              activate
              set newNote to make new note with properties {name:\(asLiteral(name)), body:\(asLiteral(body))}
              return (name of newNote) & linefeed & (body of newNote)
            end tell
            """
            let ran = runAppleScript(script, timeout: 4)
            if !ran.ok {
                return notesCreateViaUI(body, requestId: requestId)
            }
            let parts = ran.text.components(separatedBy: "\n")
            lastNoteName = parts.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? name
            let confirmedBody = parts.dropFirst().joined(separator: "\n")
            if !confirmedBody.contains(body) {
                return notesCreateViaUI(body, requestId: requestId)
            }
            lastNoteBody = body
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": true,
                    "adapter": "notes",
                    "app": "Notes",
                    "action": "create",
                    "method": "semantic",
                    "note": lastNoteName as Any,
                    "body": confirmedBody,
                    "spoken": "Created a note.",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "append":
            if body.isEmpty {
                return fail("missing_text", "What should I add?", command: "app_action", requestId: requestId)
            }
            let current = lastNoteBody ?? ""
            let combined = current.isEmpty ? body : current + "\n" + body
            let target = lastNoteName ?? ""
            let script: String
            if target.isEmpty {
                script = """
                tell application id "com.apple.Notes"
                  activate
                  set theNote to note 1
                  set body of theNote to (body of theNote) & linefeed & \(asLiteral(body))
                  return body of theNote
                end tell
                """
            } else {
                script = """
                tell application id "com.apple.Notes"
                  activate
                  set theNote to note \(asLiteral(target))
                  set body of theNote to (body of theNote) & linefeed & \(asLiteral(body))
                  return body of theNote
                end tell
                """
            }
            let ran = runAppleScript(script)
            if !ran.ok {
                return notesCreateViaUI(body, requestId: requestId, append: true)
            }
            lastNoteBody = combined
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": ran.text.contains(body),
                    "adapter": "notes",
                    "app": "Notes",
                    "action": "append",
                    "body": ran.text,
                    "spoken": "Added that line.",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "read", "status":
            let raw = notesNowText()
            let visible = notesVisibleText(raw)
            let fallback = lastNoteBody?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let body = visible.isEmpty ? fallback : visible
            // An empty front note is a successful read, not an access deny.
            // Walking other notes would speak the wrong document.
            let spoken = body.isEmpty ? "The front note is empty." : "The note says: \(body)"
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": true,
                    "adapter": "notes",
                    "app": "Notes",
                    "action": action == "status" ? "status" : "read",
                    "body": body,
                    "spoken": spoken,
                ],
                command: "app_action",
                requestId: requestId
            )
        default:
            return fail("unknown_action", "Notes cannot do that.", command: "app_action", requestId: requestId)
        }
    }

    private func notesNowText() -> String {
        let script = """
        tell application id "com.apple.Notes"
          if (count of notes) is 0 then return ""
          set raw to ""
          try
            set raw to plaintext of note 1
          end try
          if raw is missing value then set raw to ""
          if raw is "" then
            try
              set raw to body of note 1
            end try
          end if
          if raw is missing value then set raw to ""
          if raw is "" then
            try
              set selName to name of item 1 of selection
              try
                set raw to plaintext of note selName
              end try
              if raw is missing value then set raw to ""
              if raw is "" then set raw to body of note selName
            end try
          end if
          if raw is missing value then return ""
          return raw as string
        end tell
        """
        let ran = runAppleScript(script, timeout: 8)
        if ran.ok { return ran.text }
        return ""
    }

    private func notesVisibleText(_ raw: String) -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty || text == "missing value" { return "" }
        if let regex = try? NSRegularExpression(pattern: "<[^>]+>", options: []) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            text = regex.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: "")
        }
        for (escaped, value) in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")] {
            text = text.replacingOccurrences(of: escaped, with: value)
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func notesCreateViaUI(_ body: String, requestId: String, append: Bool = false) -> [String: Any] {
        _ = runAppleScript("tell application id \"com.apple.Notes\" to activate")
        Thread.sleep(forTimeInterval: 0.35)
        if !append {
            _ = postHotkey("cmd+n")
            Thread.sleep(forTimeInterval: 0.45)
        }
        let entered = enterText(body, mode: append ? "append" : "insert", element: nil, requestId: requestId)
        let needle = asLiteral(body)
        let read = runAppleScript("""
        tell application id "com.apple.Notes"
          repeat with n in notes
            try
              if (body of n) contains \(needle) then return body of n
            end try
          end repeat
          if (count of notes) is 0 then return ""
          return body of note 1
        end tell
        """, timeout: 6)
        let verified = read.text.contains(body)
        lastNoteBody = body
        return ok(
            [
                "ok": verified || (entered["ok"] as? Bool == true),
                "executed": true,
                "verified": verified,
                "adapter": "notes",
                "app": "Notes",
                "action": append ? "append" : "create",
                "method": entered["method"] as Any,
                "body": read.text,
                "spoken": verified
                    ? (append ? "Added that line." : "Created a note.")
                    : "I opened Notes but could not confirm the text.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func finderAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "open_item").lowercased()
        if action == "status" {
            return finderStatus(requestId: requestId)
        }
        if action == "close" || action == "quit" || action == "hide" || action == "close_windows" {
            if let resolved = resolve(name: "Finder", bundleId: "com.apple.finder") {
                return dismissAppWindows(resolved, requestId: requestId)
            }
            return fail("not_found", "I couldn't find Finder.", command: "close_app", requestId: requestId)
        }
        _ = runAppleScript("tell application id \"com.apple.finder\" to activate")
        let target = (string(arguments, "query") ?? string(arguments, "value") ?? string(arguments, "path") ?? "")
        let lower = target.lowercased()
        let wantsVideo = action == "play"
            || lower.contains("video")
            || lower.contains("movie")
            || lower.contains("clip")
            || ["mp4", "mov", "m4v", "mkv", "avi", "webm"].contains(where: { lower.contains($0) })
        if wantsVideo {
            return finderPlayVideo(query: target, requestId: requestId)
        }
        if lower.contains("pdf") {
            return finderOpenNewestPDF(requestId: requestId)
        }
        if lower.contains("download") || action == "open_folder" || action == "open" {
            let script = """
            tell application id "com.apple.finder"
              activate
              open (path to downloads folder)
              return POSIX path of (path to downloads folder)
            end tell
            """
            let ran = runAppleScript(script)
            return ok(
                [
                    "ok": ran.ok,
                    "executed": ran.ok,
                    "verified": ran.text.lowercased().contains("download"),
                    "adapter": "finder",
                    "app": "Finder",
                    "action": "open_folder",
                    "path": ran.text,
                    "spoken": ran.ok ? "Opened Downloads." : "I couldn't open Downloads.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: ran.ok
            )
        }
        if action == "open_item" || action == "play" {
            return finderPlayVideo(query: target, requestId: requestId)
        }
        return finderOpenNewestPDF(requestId: requestId)
    }

    private func finderOpenNewestPDF(requestId: String) -> [String: Any] {
        let script = """
        tell application id "com.apple.finder"
          activate
          set dl to (path to downloads folder) as alias
          set pdfs to (files of folder dl whose name extension is "pdf")
          if (count of pdfs) is 0 then return "NONE"
          set newest to item 1 of pdfs
          repeat with f in pdfs
            try
              if (modification date of f) > (modification date of newest) then set newest to f
            end try
          end repeat
          open newest
          return name of newest
        end tell
        """
        let ran = runAppleScript(script)
        if !ran.ok {
            return fail("finder_failed", ran.error ?? "Finder failed.", command: "app_action", requestId: requestId)
        }
        let name = ran.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if name == "NONE" || name.isEmpty {
            return ok(
                [
                    "ok": false,
                    "executed": true,
                    "verified": false,
                    "error": "not_found",
                    "adapter": "finder",
                    "app": "Finder",
                    "spoken": "There isn't a PDF in Downloads.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: false
            )
        }
        return ok(
            [
                "ok": true,
                "executed": true,
                "verified": true,
                "adapter": "finder",
                "app": "Finder",
                "action": "open_item",
                "file": name,
                "spoken": "Opened \(name).",
            ],
            command: "app_action",
            requestId: requestId
        )
    }

    private func finderSearchFolders() -> [URL] {
        var folders: [URL] = []
        if let front = finderFrontFolder() { folders.append(front) }
        let names: [FileManager.SearchPathDirectory] = [
            .downloadsDirectory, .desktopDirectory, .moviesDirectory, .documentDirectory
        ]
        for name in names {
            if let url = FileManager.default.urls(for: name, in: .userDomainMask).first {
                folders.append(url)
            }
        }
        var seen = Set<String>()
        return folders.filter { seen.insert($0.path).inserted }
    }

    private func finderFrontFolder() -> URL? {
        let script = """
        tell application id "com.apple.finder"
          if (count of windows) is 0 then return ""
          try
            return POSIX path of (target of window 1 as alias)
          end try
          return ""
        end tell
        """
        let ran = runAppleScript(script)
        let path = ran.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path)
    }

    private func mdfindMovies(in folders: [URL]) -> [URL] {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/mdfind")
        var args: [String] = []
        for folder in folders.prefix(6) {
            args.append(contentsOf: ["-onlyin", folder.path])
        }
        args.append("kMDItemContentTypeTree == 'public.movie'")
        proc.arguments = args
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do {
            try proc.run()
        } catch {
            return []
        }
        let deadline = Date().addingTimeInterval(8)
        while proc.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if proc.isRunning {
            proc.terminate()
            return []
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let text = String(data: data, encoding: .utf8) ?? ""
        let exts: Set<String> = ["mp4", "mov", "m4v", "mkv", "avi", "webm", "mpg", "mpeg"]
        return text.split(whereSeparator: \.isNewline).compactMap { line in
            let path = String(line)
            if path.isEmpty { return nil }
            let url = URL(fileURLWithPath: path)
            return exts.contains(url.pathExtension.lowercased()) ? url : nil
        }
    }

    private func enumerateVideos(in folder: URL, limit: Int) -> [URL] {
        let exts: Set<String> = ["mp4", "mov", "m4v", "mkv", "avi", "webm", "mpg", "mpeg"]
        guard let enumerator = FileManager.default.enumerator(
            at: folder,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [.skipsHiddenFiles, .skipsPackageDescendants]
        ) else { return [] }
        var out: [URL] = []
        var visited = 0
        for case let url as URL in enumerator {
            visited += 1
            if visited > 800 { break }
            if exts.contains(url.pathExtension.lowercased()) {
                out.append(url)
            }
            if out.count >= limit { break }
        }
        return out
    }

    private func pickLocalVideo(query: String, urls: [URL]) -> URL? {
        let needle = mediaNeedle(from: query).lowercased()
        let ordinal = mediaOrdinal(from: query)
        func mtime(_ url: URL) -> Date {
            ((try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate) ?? .distantPast
        }
        var ranked = urls.sorted { mtime($0) > mtime($1) }
        if !needle.isEmpty {
            let matched = ranked.filter { $0.lastPathComponent.lowercased().contains(needle) }
            if !matched.isEmpty { ranked = matched }
        }
        let index = max(1, ordinal) - 1
        if ranked.indices.contains(index) { return ranked[index] }
        return ranked.first
    }

    private func looksLikeLocalVideoPlayer(name: String, bundle: String) -> Bool {
        let blob = (name + " " + bundle).lowercased()
        return blob.contains("quicktime")
            || blob.contains("quicktimeplayer")
            || blob.contains("iina")
            || blob.contains("vlc")
            || blob.contains("com.apple.tv")
            || blob.contains("iina.io")
            || blob.contains("org.videolan")
    }

    private func defaultPlayerBundle(for url: URL) -> String? {
        guard let appURL = NSWorkspace.shared.urlForApplication(toOpen: url) else { return nil }
        return Bundle(url: appURL)?.bundleIdentifier
    }

    private func waitForVideoPlayer(preferred: String?) -> String? {
        let skip: Set<String> = ["com.ev.suit", "com.apple.finder"]
        let deadline = Date().addingTimeInterval(8)
        while Date() < deadline {
            if let preferred,
               let app = NSRunningApplication.runningApplications(withBundleIdentifier: preferred).first,
               !app.isTerminated
            {
                app.activate(options: [.activateAllWindows])
                return preferred
            }
            if let front = NSWorkspace.shared.frontmostApplication,
               let bid = front.bundleIdentifier,
               !skip.contains(bid)
            {
                return bid
            }
            Thread.sleep(forTimeInterval: 0.15)
        }
        return preferred
    }

    private func axWalkBundle(_ bundleId: String, maxWalk: Int = 280) -> [WalkItem] {
        guard AXIsProcessTrusted(),
              let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).first
        else { return [] }
        let appEl = AXUIElementCreateApplication(running.processIdentifier)
        var walked: [WalkItem] = []
        var walkCount = 0
        let windows = axElements(appEl, kAXWindowsAttribute)
        for root in windows.prefix(2) {
            walk(root, depth: 0, deadline: Date().addingTimeInterval(1.0), maxWalk: maxWalk, query: "", into: &walked, walkedCount: &walkCount)
        }
        return walked
    }

    private func axHasPauseControl(bundleId: String) -> Bool {
        for item in axWalkBundle(bundleId) {
            let blob = (item.title + " " + item.value).lowercased()
            if blob == "pause" || blob.hasPrefix("pause") || blob.contains("pause video") {
                return true
            }
        }
        return false
    }

    private func axPressPlayControl(bundleId: String) -> Bool {
        let labels = ["play", "play video", "play now", "resume"]
        for item in axWalkBundle(bundleId) where item.enabled {
            let blob = (item.title + " " + item.value).lowercased()
            let role = item.role.lowercased()
            guard role.contains("button") || item.actions.contains("AXPress") else { continue }
            if labels.contains(where: { blob == $0 || blob.hasPrefix($0) }) {
                if AXUIElementPerformAction(item.element, kAXPressAction as CFString) == .success {
                    return true
                }
                if let point = hidCenter(of: item.element) {
                    click(point)
                    return true
                }
            }
        }
        return false
    }

    private func clickLocalPlayerRegion(bundleId: String) {
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).first else { return }
        app.activate(options: [.activateAllWindows])
        Thread.sleep(forTimeInterval: 0.1)
        let appEl = AXUIElementCreateApplication(app.processIdentifier)
        guard let window = axElements(appEl, kAXWindowsAttribute).first,
              let origin = axPoint(window),
              let size = axSize(window),
              size.width > 160, size.height > 120
        else { return }
        let cocoa = CGPoint(x: origin.x + size.width * 0.5, y: origin.y + size.height * 0.45)
        click(hidFromCocoa(cocoa))
    }

    private func quickTimeDocumentCount() -> (count: Int, error: String?) {
        let ran = runAppleScript("""
        tell application id "com.apple.QuickTimePlayerX"
          return (count of documents) as text
        end tell
        """, timeout: 4)
        let n = Int(ran.text.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0
        return (n, ran.error)
    }

    private func quickTimePlayDocument(matching fileName: String) -> (state: String, name: String, error: String?) {
        let script = """
        tell application id "com.apple.QuickTimePlayerX"
          activate
          if (count of documents) is 0 then return "none||"
          set d to document 1
          set needle to \(asLiteral(fileName))
          if needle is not "" then
            set theCount to count of documents
            repeat with i from 1 to theCount
              try
                set candidate to document i
                if (name of candidate) contains needle then
                  set d to candidate
                  exit repeat
                end if
              end try
            end repeat
          end if
          play d
          delay 0.7
          set stateText to "paused"
          try
            if playing of d then set stateText to "playing"
          end try
          return stateText & "||" & (name of d)
        end tell
        """
        let ran = runAppleScript(script, timeout: 10)
        let parts = ran.text.components(separatedBy: "||")
        let state = parts.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let name = parts.count > 1 ? parts[1].trimmingCharacters(in: .whitespacesAndNewlines) : ""
        return (state.isEmpty ? "none" : state, name, ran.error)
    }

    private func ensureLocalPlayerPlaying(bundleId: String) -> String {
        if bundleId == "com.apple.QuickTimePlayerX" {
            let qt = quickTimePlayDocument(matching: "")
            if qt.state == "playing" { return "playing" }
        }
        if axHasPauseControl(bundleId: bundleId) { return "playing" }
        _ = axPressPlayControl(bundleId: bundleId)
        Thread.sleep(forTimeInterval: 0.35)
        if axHasPauseControl(bundleId: bundleId) { return "playing" }
        clickLocalPlayerRegion(bundleId: bundleId)
        Thread.sleep(forTimeInterval: 0.12)
        _ = postHotkey("space")
        Thread.sleep(forTimeInterval: 0.45)
        if axHasPauseControl(bundleId: bundleId) { return "playing" }
        if bundleId == "com.apple.QuickTimePlayerX" {
            let again = quickTimePlayDocument(matching: "")
            if again.state == "playing" { return "playing" }
        }
        return axHasPauseControl(bundleId: bundleId) ? "playing" : "paused"
    }

    private func finderPlayVideo(query: String, requestId: String) -> [String: Any] {
        let folders = finderSearchFolders()
        var urls = mdfindMovies(in: folders)
        if urls.isEmpty {
            for folder in folders.prefix(5) {
                urls.append(contentsOf: enumerateVideos(in: folder, limit: 60))
            }
        }
        guard let picked = pickLocalVideo(query: query, urls: urls) else {
            return ok(
                [
                    "ok": false,
                    "executed": true,
                    "verified": false,
                    "error": "not_found",
                    "adapter": "finder",
                    "app": "Finder",
                    "action": "play",
                    "spoken": "I couldn't find a video file to play.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: false
            )
        }
        let preferred = defaultPlayerBundle(for: picked) ?? "com.apple.QuickTimePlayerX"
        let opened = NSWorkspace.shared.open(picked)
        var appleError: String?
        var method = opened ? "open" : "open_failed"
        var state = "none"
        var title = picked.lastPathComponent
        let bundleId = waitForVideoPlayer(preferred: preferred) ?? preferred
        if let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).first {
            app.activate(options: [.activateAllWindows])
        }
        if bundleId == "com.apple.QuickTimePlayerX" {
            for _ in 0..<20 {
                let docs = quickTimeDocumentCount()
                if let err = docs.error { appleError = err }
                if docs.count > 0 { break }
                Thread.sleep(forTimeInterval: 0.25)
            }
            let played = quickTimePlayDocument(matching: picked.lastPathComponent)
            if let err = played.error { appleError = err }
            state = played.state
            if !played.name.isEmpty { title = played.name }
            if state == "playing" { method = "quicktime_play" }
        }
        if state != "playing" {
            let axState = ensureLocalPlayerPlaying(bundleId: bundleId)
            if axState == "playing" {
                state = "playing"
                if method != "quicktime_play" { method = "ax_play" }
            } else if state == "none" || state.isEmpty {
                state = axState
                method = "ax_play"
            }
        }
        let verified = state == "playing"
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "must_continue": !verified,
                "adapter": "finder",
                "app": "Finder",
                "action": "play",
                "file": picked.lastPathComponent,
                "path": picked.path,
                "player_bundle": bundleId,
                "player_state": state.isEmpty ? "none" : state,
                "title": title.isEmpty ? picked.lastPathComponent : title,
                "method": method,
                "apple_error": appleError ?? "",
                "spoken": verified
                    ? "Playing \(picked.lastPathComponent)."
                    : "Opened \(picked.lastPathComponent) but I couldn't confirm playback.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func finderStatus(requestId: String) -> [String: Any] {
        let script = """
        tell application id "com.apple.finder"
          activate
          if (count of windows) is 0 then return "Finder||"
          set theWin to window 1
          set theName to name of theWin
          set thePath to ""
          try
            set thePath to POSIX path of (target of theWin as alias)
          end try
          return theName & "||" & thePath
        end tell
        """
        let ran = runAppleScript(script)
        let parts = ran.text.components(separatedBy: "||")
        let path = parts.count > 1 ? parts[1] : ""
        return ok(
            [
                "ok": ran.ok,
                "executed": true,
                "verified": ran.ok,
                "adapter": "finder",
                "app": "Finder",
                "action": "status",
                "window": parts.first as Any,
                "path": path,
                "spoken": path.isEmpty ? "Finder is open." : "Finder is at \(path).",
            ],
            command: "app_action",
            requestId: requestId,
            ok: ran.ok
        )
    }

    private func chromeNow() -> (url: String, title: String) {
        let script = """
        tell application id "com.google.Chrome"
          if (count of windows) is 0 then return "||"
          set theTab to active tab of front window
          return (URL of theTab) & "||" & (title of theTab)
        end tell
        """
        let ran = runAppleScript(script)
        let parts = ran.text.components(separatedBy: "||")
        return (parts.first ?? "", parts.count > 1 ? parts[1] : "")
    }

    private func chromeAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "status").lowercased()
        if let chrome = chromeBrowserChrome(action, requestId: requestId) {
            return chrome
        }
        switch action {
        case "status", "read":
            let now = chromeNow()
            return ok(
                [
                    "adapter": "chrome",
                    "app": "Chrome",
                    "action": "status",
                    "url": now.url,
                    "title": now.title,
                    "spoken": now.title.isEmpty ? "Chrome is open." : "Chrome is showing \(now.title).",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "search":
            let query = searchQuery(arguments)
            if query.isEmpty {
                return fail("missing_query", "What should I search for?", command: "app_action", requestId: requestId)
            }
            if let dest = Self.navigationURL(from: query) {
                return chromeOpenLocation(dest, requestId: requestId)
            }
            lastChromeQuery = query
            let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
            let target = "https://www.google.com/search?q=\(encoded)"
            let script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then
                make new window
                set URL of active tab of front window to \(asLiteral(target))
              else
                tell front window
                  make new tab with properties {URL:\(asLiteral(target))}
                end tell
              end if
            end tell
            """
            let ran = runAppleScript(script)
            if !ran.ok {
                return fail("chrome_failed", ran.error ?? "Chrome failed.", command: "app_action", requestId: requestId)
            }
            Thread.sleep(forTimeInterval: 0.8)
            var now = chromeNow()
            for _ in 0..<8 where now.url.isEmpty {
                Thread.sleep(forTimeInterval: 0.35)
                now = chromeNow()
            }
            let loaded = !now.url.isEmpty
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": loaded,
                    "must_continue": !loaded,
                    "adapter": "chrome",
                    "app": "Chrome",
                    "action": "search",
                    "query": query,
                    "url": loaded ? now.url : target,
                    "title": now.title,
                    "spoken": "Chrome is showing search results for \(query).",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "navigate", "open_item":
            let query = locationQuery(arguments)
            if let dest = Self.navigationURL(from: query) {
                return chromeOpenLocation(dest, requestId: requestId)
            }
            if queryLooksLikeMedia(query) {
                return playMediaInBrowser(browser: "chrome", arguments: arguments, requestId: requestId)
            }
            return chromeOpenFirstResult(arguments, requestId: requestId)
        case "play":
            return playMediaInBrowser(browser: "chrome", arguments: arguments, requestId: requestId)
        default:
            return fail("unknown_action", "Chrome cannot do that.", command: "app_action", requestId: requestId)
        }
    }

    private func chromeOpenLocation(_ url: String, requestId: String) -> [String: Any] {
        let script = """
        tell application id "com.google.Chrome"
          if (count of windows) is 0 then
            make new window
            set URL of active tab of front window to \(asLiteral(url))
          else
            tell front window
              make new tab with properties {URL:\(asLiteral(url))}
            end tell
          end if
        end tell
        """
        let ran = runAppleScript(script)
        Thread.sleep(forTimeInterval: 1.0)
        var now = chromeNow()
        for _ in 0..<6 where now.url.isEmpty {
            Thread.sleep(forTimeInterval: 0.35)
            now = chromeNow()
        }
        let verified = ran.ok && !now.url.isEmpty && !now.url.lowercased().contains("google.com/search")
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "adapter": "chrome",
                "app": "Chrome",
                "action": "navigate",
                "url": now.url,
                "title": now.title,
                "spoken": verified
                    ? "Opened \(now.title.isEmpty ? now.url : now.title)."
                    : "Chrome did not open that page.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func chromeBrowserChrome(_ action: String, requestId: String) -> [String: Any]? {
        let script: String
        let spoken: String
        switch action {
        case "new_tab":
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then
                make new window
              else
                tell front window to make new tab
              end if
            end tell
            """
            spoken = "Opened a new Chrome tab."
        case "close_tab":
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then return
              tell front window
                if (count of tabs) <= 1 then
                  close
                else
                  close active tab
                end if
              end tell
            end tell
            """
            spoken = "Closed the Chrome tab."
        case "next_tab":
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then return
              tell front window
                set i to active tab index
                set n to count of tabs
                if n is 0 then return
                if i < n then
                  set active tab index to (i + 1)
                else
                  set active tab index to 1
                end if
              end tell
            end tell
            """
            spoken = "Switched to the next Chrome tab."
        case "previous_tab":
            script = """
            tell application id "com.google.Chrome"
              if (count of windows) is 0 then return
              tell front window
                set i to active tab index
                set n to count of tabs
                if n is 0 then return
                if i > 1 then
                  set active tab index to (i - 1)
                else
                  set active tab index to n
                end if
              end tell
            end tell
            """
            spoken = "Switched to the previous Chrome tab."
        default:
            return nil
        }
        let ran = runAppleScript(script)
        Thread.sleep(forTimeInterval: 0.35)
        let now = chromeNow()
        return ok(
            [
                "ok": ran.ok,
                "executed": true,
                "verified": ran.ok,
                "adapter": "chrome",
                "app": "Chrome",
                "action": action,
                "url": now.url,
                "title": now.title,
                "must_continue": false,
                "goal_complete": ran.ok,
                "spoken": ran.ok ? spoken : "Chrome could not do that tab action.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: ran.ok
        )
    }

    private func chromeOpenFirstResult(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let query = firstResultSearchQuery(arguments, stored: lastChromeQuery) ?? ""
        if !query.isEmpty { lastChromeQuery = query }
        let start = chromeNow()
        if !isSearchResultsURL(start.url) {
            Thread.sleep(forTimeInterval: 1.2)
            var href = clickFirstOnPageItem(browser: "chrome")
            if href.isEmpty, chromePressFirstLink() {
                Thread.sleep(forTimeInterval: 0.8)
            }
            let now = chromeNow()
            let verified = looksLikeContentItemURL(now.url)
            return ok(
                [
                    "ok": verified,
                    "executed": true,
                    "verified": verified,
                    "must_continue": !verified,
                    "adapter": "chrome",
                    "app": "Chrome",
                    "action": "navigate",
                    "url": now.url,
                    "title": now.title,
                    "clicked": href,
                    "spoken": verified
                        ? "Opened \(now.title.isEmpty ? now.url : now.title)."
                        : "I couldn't open the first item on this page.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: verified
            )
        }
        var href = extractGoogleFirstHref(browser: "chrome")
        var stillSearch = isSearchResultsURL(chromeNow().url)
        if href.hasPrefix("http"), stillSearch {
            let script = """
            tell application id "com.google.Chrome"
              set URL of active tab of front window to \(asLiteral(href))
            end tell
            """
            _ = runAppleScript(script)
            Thread.sleep(forTimeInterval: 1.0)
            stillSearch = isSearchResultsURL(chromeNow().url)
        }
        if stillSearch, let q = lastChromeQuery ?? (query.isEmpty ? nil : query),
           let discovered = discoverFirstWebResult(q)
        {
            href = discovered
            let script = """
            tell application id "com.google.Chrome"
              set URL of active tab of front window to \(asLiteral(discovered))
            end tell
            """
            _ = runAppleScript(script)
            Thread.sleep(forTimeInterval: 1.2)
            stillSearch = isSearchResultsURL(chromeNow().url)
        }
        if stillSearch, chromePressFirstLink() {
            Thread.sleep(forTimeInterval: 0.8)
            stillSearch = isSearchResultsURL(chromeNow().url)
        }
        let now = chromeNow()
        let verified = !isSearchResultsURL(now.url) && !now.url.isEmpty
        return ok(
            [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "must_continue": !verified,
                "adapter": "chrome",
                "app": "Chrome",
                "action": "navigate",
                "url": now.url,
                "title": now.title,
                "clicked": href,
                "spoken": verified
                    ? "Opened \(now.title.isEmpty ? now.url : now.title)."
                    : "Chrome is still on the search page.",
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func chromePressFirstLink() -> Bool {
        guard AXIsProcessTrusted() else { return false }
        let skip = ["images", "videos", "maps", "news", "shopping", "more", "tools", "settings", "sign in"]
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome").first
        guard let running else { return false }
        let appEl = AXUIElementCreateApplication(running.processIdentifier)
        var walked: [WalkItem] = []
        var walkCount = 0
        let windows = axElements(appEl, kAXWindowsAttribute)
        for root in windows.prefix(2) {
            walk(root, depth: 0, deadline: Date().addingTimeInterval(1.4), maxWalk: 420, query: "", into: &walked, walkedCount: &walkCount)
        }
        let links = walked.filter { $0.role == "AXLink" && $0.enabled && !$0.title.isEmpty }
            .filter { item in
                let blob = item.title.lowercased()
                return !skip.contains(where: { blob == $0 || blob.hasPrefix($0 + " ") })
                    && !blob.contains("google.com/search")
                    && blob != "about this result"
            }
        if let first = links.first {
            if itemSupportsPress(first) {
                if AXUIElementPerformAction(first.element, kAXPressAction as CFString) == .success {
                    return true
                }
            }
            if let point = hidCenter(of: first.element) {
                click(point)
                return true
            }
        }
        return false
    }

    private func spotifyNow() -> (state: String, track: String, artist: String) {
        let script = """
        tell application id "com.spotify.client"
          set stateText to "unknown"
          try
            if player state is playing then
              set stateText to "playing"
            else if player state is paused then
              set stateText to "paused"
            else if player state is stopped then
              set stateText to "stopped"
            end if
          end try
          set trackName to ""
          set artistName to ""
          try
            set trackName to name of current track
            set artistName to artist of current track
          end try
          return stateText & linefeed & trackName & linefeed & artistName
        end tell
        """
        let ran = runAppleScript(script)
        let parts = ran.text.split(separator: "\n", omittingEmptySubsequences: false).map { String($0) }
        let state = parts.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "unknown"
        let track = parts.count > 1 ? parts[1] : ""
        let artist = parts.count > 2 ? parts[2] : ""
        return (state, track, artist)
    }

    private func spotifyAction(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "status").lowercased()
        switch action {
        case "status", "current", "now_playing":
            let now = spotifyNow()
            let spoken: String
            if now.state == "playing" && !now.track.isEmpty {
                spoken = "Playing \(now.track)."
            } else if now.state == "paused" && !now.track.isEmpty {
                spoken = "Spotify is paused on \(now.track)."
            } else {
                spoken = "Spotify is \(now.state)."
            }
            return ok(
                [
                    "adapter": "spotify",
                    "app": "Spotify",
                    "action": "status",
                    "player_state": now.state,
                    "track": now.track,
                    "artist": now.artist,
                    "spoken": spoken,
                ],
                command: "app_action",
                requestId: requestId
            )
        case "play", "play_track", "play_playlist", "open_item":
            let query = string(arguments, "query")
                ?? string(arguments, "track")
                ?? string(arguments, "value")
                ?? string(arguments, "playlist")
                ?? ""
            if !query.isEmpty {
                return spotifySearchThenPlay(query, requestId: requestId)
            }
            _ = runAppleScript("tell application id \"com.spotify.client\" to play")
            Thread.sleep(forTimeInterval: 0.4)
            let now = spotifyNow()
            let verified = now.state == "playing"
            return ok(
                [
                    "ok": verified,
                    "executed": true,
                    "verified": verified,
                    "adapter": "spotify",
                    "app": "Spotify",
                    "action": "play",
                    "player_state": now.state,
                    "track": now.track,
                    "artist": now.artist,
                    "spoken": verified
                        ? (now.track.isEmpty ? "Spotify is playing." : "Playing \(now.track).")
                        : "I told Spotify to play, but it is \(now.state).",
                ],
                command: "app_action",
                requestId: requestId,
                ok: verified
            )
        case "pause":
            _ = runAppleScript("tell application id \"com.spotify.client\" to pause")
            Thread.sleep(forTimeInterval: 0.25)
            let now = spotifyNow()
            return ok(
                [
                    "adapter": "spotify",
                    "app": "Spotify",
                    "action": "pause",
                    "player_state": now.state,
                    "track": now.track,
                    "spoken": "Paused.",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "next":
            _ = runAppleScript("tell application id \"com.spotify.client\" to next track")
            Thread.sleep(forTimeInterval: 0.4)
            let now = spotifyNow()
            return ok(
                [
                    "adapter": "spotify",
                    "app": "Spotify",
                    "action": "next",
                    "player_state": now.state,
                    "track": now.track,
                    "spoken": now.track.isEmpty ? "Skipped." : "Playing \(now.track).",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "previous", "back":
            _ = runAppleScript("tell application id \"com.spotify.client\" to previous track")
            Thread.sleep(forTimeInterval: 0.4)
            let now = spotifyNow()
            return ok(
                [
                    "adapter": "spotify",
                    "app": "Spotify",
                    "action": "previous",
                    "player_state": now.state,
                    "track": now.track,
                    "spoken": now.track.isEmpty ? "Went back." : "Playing \(now.track).",
                ],
                command: "app_action",
                requestId: requestId
            )
        case "search":
            let query = string(arguments, "query") ?? string(arguments, "value") ?? string(arguments, "playlist") ?? ""
            if query.isEmpty {
                return fail("missing_query", "What should I search for on Spotify?", command: "app_action", requestId: requestId)
            }
            let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
            guard let url = URL(string: "spotify:search:\(encoded)") else {
                return fail("invalid_url", "I couldn't build that Spotify search.", command: "app_action", requestId: requestId)
            }
            _ = runAppleScript("tell application id \"com.spotify.client\" to activate")
            let opened = NSWorkspace.shared.open(url)
            Thread.sleep(forTimeInterval: 0.8)
            return ok(
                [
                    "ok": opened,
                    "executed": true,
                    "verified": false,
                    "must_continue": true,
                    "adapter": "spotify",
                    "app": "Spotify",
                    "action": "search",
                    "query": query,
                    "url": url.absoluteString,
                    "spoken": opened
                        ? "Spotify is showing search results for \(query). Playing a result is still required."
                        : "I couldn't open Spotify search.",
                ],
                command: "app_action",
                requestId: requestId,
                ok: opened
            )
        default:
            return fail("unknown_action", "Spotify cannot do that.", command: "app_action", requestId: requestId)
        }
    }

    private func spotifySearchThenPlay(_ query: String, requestId: String) -> [String: Any] {
        let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
        guard let url = URL(string: "spotify:search:\(encoded)") else {
            return fail("invalid_url", "I couldn't build that Spotify search.", command: "app_action", requestId: requestId)
        }
        _ = runAppleScript("tell application id \"com.spotify.client\" to activate")
        let opened = NSWorkspace.shared.open(url)
        Thread.sleep(forTimeInterval: 0.9)
        _ = runAppleScript("""
        tell application "System Events"
          tell process "Spotify"
            set frontmost to true
            keystroke return
          end tell
        end tell
        """)
        Thread.sleep(forTimeInterval: 1.1)
        let now = spotifyNow()
        let wanted = query.lowercased()
        let playing = now.state == "playing" && !now.track.isEmpty
        let matched = playing && (
            now.track.lowercased().contains(wanted)
                || wanted.contains(now.track.lowercased())
                || now.artist.lowercased().contains(wanted)
        )
        let verified = playing
        let spoken: String
        if matched {
            spoken = "Playing \(now.track)."
        } else if verified {
            spoken = "Spotify is playing \(now.track)."
        } else if opened {
            spoken = "Spotify opened search for \(query), but I could not confirm playback."
        } else {
            spoken = "I couldn't open Spotify search."
        }
        return ok(
            [
                "ok": verified,
                "executed": opened,
                "verified": verified,
                "must_continue": !verified,
                "adapter": "spotify",
                "app": "Spotify",
                "action": "play",
                "query": query,
                "player_state": now.state,
                "track": now.track,
                "artist": now.artist,
                "spoken": spoken,
            ],
            command: "app_action",
            requestId: requestId,
            ok: verified
        )
    }

    private func musicNowPlaying() -> (state: String, track: String, artist: String) {
        let script = """
        tell application id "com.apple.Music"
          set stateText to "unknown"
          try
            if player state is playing then
              set stateText to "playing"
            else if player state is paused then
              set stateText to "paused"
            else if player state is stopped then
              set stateText to "stopped"
            else if player state is fast forwarding then
              set stateText to "fast forwarding"
            else if player state is rewinding then
              set stateText to "rewinding"
            end if
          end try
          set trackName to ""
          set artistName to ""
          try
            set trackName to name of current track
            set artistName to artist of current track
          end try
          return stateText & linefeed & trackName & linefeed & artistName
        end tell
        """
        let ran = runAppleScript(script)
        let parts = ran.text.split(separator: "\n", omittingEmptySubsequences: false).map { String($0) }
        let state = parts.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "unknown"
        let track = parts.count > 1 ? parts[1] : ""
        let artist = parts.count > 2 ? parts[2] : ""
        return (state, track, artist)
    }

    private func musicStatus(requestId: String, spokenPrefix: String?) -> [String: Any] {
        let now = musicNowPlaying()
        let playing = now.state == "playing"
        let spoken: String
        if let spokenPrefix, !spokenPrefix.isEmpty {
            spoken = spokenPrefix
        } else if playing {
            spoken = now.track.isEmpty ? "Music is playing." : "Playing \(now.track)."
        } else {
            spoken = "Music is \(now.state)."
        }
        return ok(
            [
                "ok": true,
                "executed": true,
                "verified": playing,
                "adapter": "music",
                "app": "Music",
                "player_state": now.state,
                "track": now.track,
                "artist": now.artist,
                "playlist": lastPlaylist as Any,
                "index": lastTrackIndex as Any,
                "action": "status",
                "spoken": spoken,
            ],
            command: "app_action",
            requestId: requestId
        )
    }

    private func musicScriptFail(_ message: String?, requestId: String) -> [String: Any] {
        let raw = (message ?? "Music scripting failed")
        let blocked = raw.lowercased().contains("not allowed") || raw.contains("-1743") || raw.lowercased().contains("access")
        let spoken = blocked
            ? "Music automation is blocked. Allow EV to control Music in System Settings."
            : "Music did not accept that command."
        return ok(
            [
                "ok": false,
                "executed": false,
                "verified": false,
                "error": blocked ? "automation_denied" : "music_script_failed",
                "adapter": "music",
                "app": "Music",
                "spoken": spoken,
            ],
            command: "app_action",
            requestId: requestId,
            ok: false
        )
    }

    // MARK: - App resolve

    private struct ResolvedApp {
        let name: String
        let bundle: String
        let url: URL?
        let running: NSRunningApplication?
    }

    private func isHelperProcess(_ app: NSRunningApplication) -> Bool {
        let name = (app.localizedName ?? "").lowercased()
        let markers = ["service", "helper", "webcontent", "gpu", "renderer", "uiview", "plugin", "crashpad"]
        if markers.contains(where: { name.contains($0) }) { return true }
        return app.activationPolicy != .regular
    }

    private func pickUIProcess(bundleId: String) -> NSRunningApplication? {
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
        if apps.isEmpty { return nil }
        if let active = apps.first(where: { $0.isActive && !isHelperProcess($0) }) {
            return active
        }
        let regular = apps.filter { $0.activationPolicy == .regular }
        func windowCount(_ app: NSRunningApplication) -> Int {
            axElements(AXUIElementCreateApplication(app.processIdentifier), kAXWindowsAttribute).count
        }
        if let best = regular.max(by: { windowCount($0) < windowCount($1) }), windowCount(best) > 0 {
            return best
        }
        return regular.first ?? apps.first(where: { !isHelperProcess($0) }) ?? apps.first
    }

    private func resolve(name: String?, bundleId: String?) -> ResolvedApp? {
        if let bundleId, !bundleId.isEmpty {
            let running = pickUIProcess(bundleId: bundleId)
            let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId)
            let display = running?.localizedName ?? url?.deletingPathExtension().lastPathComponent ?? bundleId
            if running != nil || url != nil {
                return ResolvedApp(name: display, bundle: bundleId, url: url, running: running)
            }
        }
        let raw = (name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else {
            if let front = NSWorkspace.shared.frontmostApplication, let bundle = front.bundleIdentifier {
                return ResolvedApp(name: front.localizedName ?? bundle, bundle: bundle, url: front.bundleURL, running: front)
            }
            return nil
        }
        var key = Self.aliases[raw.lowercased()] ?? raw.lowercased()
        if key.hasPrefix("the ") { key = String(key.dropFirst(4)) }
        if key.hasPrefix("app ") { key = String(key.dropFirst(4)) }
        if key.hasSuffix(" app") { key = String(key.dropLast(4)) }
        if let bundle = Self.known[key] {
            return resolve(name: nil, bundleId: bundle)
        }
        let running = NSWorkspace.shared.runningApplications.first {
            guard !isHelperProcess($0) else { return false }
            return ($0.localizedName ?? "").lowercased() == key
                || ($0.bundleIdentifier ?? "").lowercased() == key
        }
        if let running, let bundle = running.bundleIdentifier {
            return ResolvedApp(
                name: running.localizedName ?? bundle,
                bundle: bundle,
                url: running.bundleURL,
                running: pickUIProcess(bundleId: bundle) ?? running
            )
        }
        let installedExact = installedApps().first {
            ($0["name"] as? String)?.lowercased() == key
                || ($0["bundle_id"] as? String)?.lowercased() == key
        }
        let installed = installedExact ?? installedApps().first {
            ($0["name"] as? String)?.lowercased().contains(key) == true
        }
        if let installed, let bundle = installed["bundle_id"] as? String {
            return resolve(name: nil, bundleId: bundle)
        }
        return nil
    }

    private func runningApps() -> [[String: Any]] {
        NSWorkspace.shared.runningApplications.compactMap { app in
            guard let bundle = app.bundleIdentifier, app.activationPolicy != .prohibited else { return nil }
            return [
                "name": app.localizedName as Any,
                "bundle_id": bundle,
                "pid": app.processIdentifier,
                "running": true,
                "frontmost": app.isActive,
                "path": app.bundleURL?.path as Any,
            ]
        }
    }

    private func applicationsStamp() -> String {
        let directories = [
            "/Applications",
            "/System/Applications",
            NSHomeDirectory() + "/Applications",
        ]
        return directories.map { path in
            let url = URL(fileURLWithPath: path)
            let date = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            return "\(path):\(date?.timeIntervalSince1970 ?? 0)"
        }.joined(separator: "|")
    }

    private func startAppCatalogWatch() {
        let paths = [
            "/Applications",
            "/System/Applications",
            NSHomeDirectory() + "/Applications",
        ]
        for path in paths {
            let fd = open(path, O_EVTONLY)
            guard fd >= 0 else { continue }
            let source = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: fd,
                eventMask: [.write, .rename, .delete, .attrib],
                queue: DispatchQueue.global(qos: .utility)
            )
            source.setEventHandler { [weak self] in
                self?.installedAppsCache = nil
            }
            source.setCancelHandler {
                close(fd)
            }
            source.resume()
            appDirSources.append(source)
        }
    }

    private func installedApps() -> [[String: Any]] {
        let stamp = applicationsStamp()
        if let cache = installedAppsCache, cache.stamp == stamp {
            return cache.apps
        }
        let directories = [
            "/Applications",
            "/System/Applications",
            NSHomeDirectory() + "/Applications",
            "/System/Volumes/Preboot/Cryptexes/App/System/Applications",
        ]
        var items: [[String: Any]] = []
        for directory in directories {
            guard let contents = try? FileManager.default.contentsOfDirectory(at: URL(fileURLWithPath: directory), includingPropertiesForKeys: nil) else { continue }
            for url in contents where url.pathExtension == "app" {
                let bundle = Bundle(url: url)
                let identifier = bundle?.bundleIdentifier ?? url.deletingPathExtension().lastPathComponent
                items.append([
                    "name": bundle?.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
                        ?? bundle?.object(forInfoDictionaryKey: "CFBundleName") as? String
                        ?? url.deletingPathExtension().lastPathComponent,
                    "bundle_id": identifier,
                    "path": url.path,
                    "running": false,
                ])
            }
        }
        installedAppsCache = (stamp, items)
        return items
    }

    private func windowInfo(pid: pid_t) -> (id: CGWindowID, bounds: CGRect, title: String)? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        for info in list {
            guard let owner = info[kCGWindowOwnerPID as String] as? pid_t, owner == pid else { continue }
            let layer = info[kCGWindowLayer as String] as? Int ?? 0
            if layer != 0 { continue }
            let number = info[kCGWindowNumber as String] as? UInt32 ?? 0
            let boundsDict = info[kCGWindowBounds as String] as? [String: Any] ?? [:]
            let bounds = CGRect(
                x: boundsDict["X"] as? CGFloat ?? 0,
                y: boundsDict["Y"] as? CGFloat ?? 0,
                width: boundsDict["Width"] as? CGFloat ?? 0,
                height: boundsDict["Height"] as? CGFloat ?? 0
            )
            let title = info[kCGWindowName as String] as? String ?? ""
            if number > 0 { return (CGWindowID(number), bounds, title) }
        }
        return nil
    }

    // MARK: - AX value helpers

    private func copy(_ element: AXUIElement, _ attribute: String) -> AnyObject? {
        var value: AnyObject?
        let status = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
        return status == .success ? value : nil
    }

    private func axElement(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
        guard let value = copy(element, attribute) else { return nil }
        return (value as! AXUIElement)
    }

    private func axElements(_ element: AXUIElement, _ attribute: String) -> [AXUIElement] {
        guard let value = copy(element, attribute) else { return [] }
        guard let array = value as? NSArray else { return [] }
        return array.map { $0 as! AXUIElement }
    }

    private func stringValue(_ element: AXUIElement, _ attribute: String) -> String? {
        guard let value = copy(element, attribute) else { return nil }
        if let text = value as? String { return text }
        if CFGetTypeID(value) == AXValueGetTypeID() { return nil }
        return String(describing: value)
    }

    private func boolValue(_ element: AXUIElement, _ attribute: String, defaultValue: Bool = false) -> Bool {
        guard let value = copy(element, attribute) else { return defaultValue }
        if let number = value as? Bool { return number }
        if let number = value as? NSNumber { return number.boolValue }
        return defaultValue
    }

    private func firstNonEmpty(_ values: String?...) -> String {
        for value in values {
            if let value, !value.isEmpty { return value }
        }
        return ""
    }

    private func string(_ arguments: [String: Any], _ key: String) -> String? {
        guard let value = arguments[key], !(value is NSNull) else { return nil }
        if let value = value as? String {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        if value is Bool || value is NSNumber { return nil }
        let trimmed = String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func searchQuery(_ arguments: [String: Any]) -> String {
        for key in ["query", "value", "text"] {
            if let value = string(arguments, key) { return Self.cleanedComputerQuery(value) }
        }
        return ""
    }

    private func locationQuery(_ arguments: [String: Any]) -> String {
        for key in ["url", "query", "value"] {
            if let value = string(arguments, key) { return Self.cleanedComputerQuery(value) }
        }
        return ""
    }

    static func cleanedComputerQuery(_ raw: String) -> String {
        var query = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        query = query.trimmingCharacters(in: CharacterSet(charactersIn: "\"'`“”"))
        query = query.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        query = query.replacingOccurrences(
            of: #"(?i)\s+continue=true\s*$"#,
            with: "",
            options: .regularExpression
        )
        query = query.replacingOccurrences(
            of: #"(?i)\s+(?:nothing else|that's it|thats it|only that|please|thanks|for me)\s*$"#,
            with: "",
            options: .regularExpression
        )
        query = query.replacingOccurrences(
            of: #"(?i)\s+in(?:\s+a|\s+the)?\s+new\s+tab\s*$"#,
            with: "",
            options: .regularExpression
        )
        query = query.replacingOccurrences(
            of: #"(?i)\s+(?:in|on|using|with)\s+(?:the\s+)?(?:google\s+)?(?:chrome|safari|browser|spotify|notes|finder)\b.*$"#,
            with: "",
            options: .regularExpression
        )
        query = query.replacingOccurrences(
            of: #"(?i)\s*(?:,|and|,?\s*then)\s+(?:open|click|press|play|go to|visit|close|quit)\b.*$"#,
            with: "",
            options: .regularExpression
        )
        query = query.trimmingCharacters(in: CharacterSet(charactersIn: " \"'"))
        if !query.contains(" "), query.contains(".") {
            query = stripGluedCommandLabels(query)
        }
        return String(query.prefix(200))
    }

    static func stripGluedCommandLabels(_ raw: String) -> String {
        let command: Set<String> = [
            "open", "in", "on", "for", "and", "then", "new", "tab", "the", "a",
            "to", "with", "using", "please", "safari", "chrome", "browser",
            "search", "play", "watch", "click", "first", "result", "nothing",
            "else", "just", "only", "app", "window", "here", "there", "now",
            "go", "visit", "launch",
        ]
        var prefix = ""
        var rest = raw
        let lower = rest.lowercased()
        for scheme in ["https://", "http://"] where lower.hasPrefix(scheme) {
            prefix = String(rest.prefix(scheme.count))
            rest = String(rest.dropFirst(scheme.count))
            break
        }
        let parts = rest.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
        var host = String(parts.first ?? "")
        var labels = host.split(separator: ".").map(String.init).filter { !$0.isEmpty }
        while labels.count >= 3, command.contains(labels[labels.count - 1].lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".,);!?"))) {
            labels.removeLast()
        }
        host = labels.joined(separator: ".")
        if host.isEmpty { return raw }
        if parts.count > 1 {
            return prefix + host + "/" + parts[1]
        }
        return prefix + host
    }

    static func navigationURL(from raw: String) -> String? {
        var trimmed = cleanedComputerQuery(raw)
        trimmed = trimmed.replacingOccurrences(
            of: #"(?<=[A-Za-z0-9])\s+\.\s+(?=[A-Za-z0-9])"#,
            with: ".",
            options: .regularExpression
        )
        let quotes = CharacterSet(charactersIn: "\"'`")
        trimmed = trimmed.trimmingCharacters(in: quotes)
        while let last = trimmed.last, ".,);!?".contains(last) {
            trimmed.removeLast()
        }
        if trimmed.isEmpty || trimmed.contains(" ") || trimmed.contains("@") { return nil }
        let lower = trimmed.lowercased()
        if lower == "localhost" || lower == "about:blank" { return nil }
        if lower.hasPrefix("http://") || lower.hasPrefix("https://") { return trimmed }
        let pattern = #"^(?:www\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}(?::\d{2,5})?(?:/[^\s]*)?$"#
        guard trimmed.range(of: pattern, options: .regularExpression) != nil else { return nil }
        return "https://\(trimmed)"
    }

    private func bool(_ arguments: [String: Any], _ key: String) -> Bool {
        if let value = arguments[key] as? Bool { return value }
        if let value = arguments[key] as? String { return ["1", "true", "yes"].contains(value.lowercased()) }
        if let value = arguments[key] as? NSNumber { return value.boolValue }
        return false
    }

    private func int(_ arguments: [String: Any], _ key: String) -> Int? {
        if let value = arguments[key] as? Int { return value }
        if let value = arguments[key] as? Double { return Int(value) }
        if let value = arguments[key] as? String { return Int(value) }
        return nil
    }

    private func double(_ arguments: [String: Any], _ key: String) -> Double? {
        if let value = arguments[key] as? Double { return value }
        if let value = arguments[key] as? Int { return Double(value) }
        if let value = arguments[key] as? String { return Double(value) }
        return nil
    }

    private func openSettingsPane(_ urlString: String, requestId: String) -> [String: Any] {
        guard let url = URL(string: urlString) else {
            return fail("not_found", "I couldn't open that Settings pane.", command: "open_app", requestId: requestId)
        }
        let opened = NSWorkspace.shared.open(url)
        Thread.sleep(forTimeInterval: 0.6)
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.systempreferences").first
            ?? NSWorkspace.shared.frontmostApplication
        running?.activate(options: [.activateAllWindows])
        lastBundle = running?.bundleIdentifier
        lastApp = running?.localizedName
        return ok(
            [
                "name": running?.localizedName ?? "System Settings",
                "app": running?.localizedName ?? "System Settings",
                "bundle_id": running?.bundleIdentifier ?? "com.apple.systempreferences",
                "opened": opened,
                "activated": true,
                "running": running != nil,
                "pane": urlString,
                "spoken": opened ? "Opened System Settings." : "I couldn't open Settings.",
                "verification_hint": "inspect the Settings pane",
            ],
            command: "open_app",
            requestId: requestId,
            ok: opened
        )
    }

    private static func settingsPaneURL(for name: String) -> String? {
        let key = aliases[name] ?? name
        return settingsPanes[key]
    }

    private func bluetoothPowered() -> Bool? {
        guard let handle = dlopen("/System/Library/Frameworks/IOBluetooth.framework/IOBluetooth", RTLD_NOW) else {
            return nil
        }
        defer { dlclose(handle) }
        guard let symbol = dlsym(handle, "IOBluetoothPreferenceGetControllerPowerState") else {
            return nil
        }
        typealias Fn = @convention(c) () -> Int32
        let fn = unsafeBitCast(symbol, to: Fn.self)
        return fn() != 0
    }

    // MARK: - Local files

    private static let fileMaxBytes = 256 * 1024
    private static let fileListCap = 40
    private static let textFileExtensions: Set<String> = [
        "txt", "md", "markdown", "json", "csv", "py", "swift", "js", "ts", "tsx",
        "jsx", "html", "css", "xml", "yml", "yaml", "toml", "ini", "log", "sh",
        "rs", "go", "rb", "java", "c", "h", "cpp", "hpp", "rtf", "text",
    ]
    private static let deniedNameParts: Set<String> = [
        ".ssh", ".aws", ".gnupg", ".kube", "keychains", "keychain",
    ]
    private static let deniedFileNames: Set<String> = [
        ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
        "id_ecdsa", "credentials.json", "credentials.csv", "master.key", "api.env",
    ]
    private static let deniedSuffixes = [".pem", ".p12", ".pfx", ".key", ".kdbx"]

    private func allowedFileRoots() -> [URL] {
        let fm = FileManager.default
        let names: [FileManager.SearchPathDirectory] = [
            .desktopDirectory, .documentDirectory, .downloadsDirectory,
            .moviesDirectory, .musicDirectory, .picturesDirectory,
        ]
        var roots: [URL] = []
        for name in names {
            if let url = fm.urls(for: name, in: .userDomainMask).first {
                roots.append(url.resolvingSymlinksInPath())
            }
        }
        let code = fm.homeDirectoryForCurrentUser.appendingPathComponent("Code", isDirectory: true)
        if fm.fileExists(atPath: code.path) {
            roots.append(code.resolvingSymlinksInPath())
        }
        return roots
    }

    private func fileDenied(_ url: URL) -> String? {
        let resolved = url.resolvingSymlinksInPath()
        let path = resolved.path
        let lowered = path.lowercased()
        for part in resolved.pathComponents {
            let name = part.lowercased()
            if Self.deniedNameParts.contains(name) || Self.deniedFileNames.contains(name) {
                return "path_denied"
            }
        }
        if Self.deniedFileNames.contains(resolved.lastPathComponent.lowercased()) {
            return "path_denied"
        }
        if Self.deniedSuffixes.contains(where: { lowered.hasSuffix($0) }) {
            return "path_denied"
        }
        if lowered.contains("/library/application support/ev")
            || lowered.contains("/.ev/secrets")
            || lowered.contains("/.git/")
        {
            return "path_denied"
        }
        if !allowedFileRoots().contains(where: { root in
            path == root.path || path.hasPrefix(root.path + "/")
        }) {
            return "path_outside_allowed"
        }
        return nil
    }

    private func expandFilePath(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }
        if trimmed.hasPrefix("~") {
            return URL(fileURLWithPath: (trimmed as NSString).expandingTildeInPath)
        }
        if trimmed.hasPrefix("/") {
            return URL(fileURLWithPath: trimmed)
        }
        if let desktop = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first {
            return desktop.appendingPathComponent(trimmed)
        }
        return URL(fileURLWithPath: trimmed)
    }

    private func locateFile(path: String, query: String) -> (url: URL?, matches: [URL], error: String?) {
        let needle = (query.isEmpty ? URL(fileURLWithPath: path).lastPathComponent : query)
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        if let url = expandFilePath(path), FileManager.default.fileExists(atPath: url.path) {
            if let denied = fileDenied(url) { return (nil, [], denied) }
            if isDirectory(url) {
                if needle.isEmpty { return (url.resolvingSymlinksInPath(), [], nil) }
            } else if needle.isEmpty || url.lastPathComponent.lowercased().contains(needle) {
                return (url.resolvingSymlinksInPath(), [], nil)
            }
        }
        if needle.isEmpty {
            if let url = expandFilePath(path) {
                if let denied = fileDenied(url) { return (nil, [], denied) }
                return (url.resolvingSymlinksInPath(), [], nil)
            }
            return (nil, [], "not_found")
        }
        var matches: [URL] = []
        let fm = FileManager.default
        var searchRoots = allowedFileRoots()
        if let scoped = expandFilePath(path), isDirectory(scoped) {
            searchRoots = [scoped]
        }
        for root in searchRoots {
            guard let children = try? fm.contentsOfDirectory(
                at: root,
                includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            ) else { continue }
            for child in children where child.lastPathComponent.lowercased().contains(needle) {
                if fileDenied(child) == nil {
                    matches.append(child.resolvingSymlinksInPath())
                }
            }
        }
        let exact = matches.filter { $0.lastPathComponent.lowercased() == needle }
        let pool = exact.isEmpty ? matches : exact
        if pool.count == 1 { return (pool[0], [], nil) }
        if pool.count > 1 {
            let ranked = pool.sorted { lhs, rhs in
                fileSearchScore(lhs, needle: needle) > fileSearchScore(rhs, needle: needle)
            }
            return (ranked[0], [], nil)
        }
        if let url = expandFilePath(path), !isDirectory(url) {
            if let denied = fileDenied(url) { return (nil, [], denied) }
            return (url.resolvingSymlinksInPath(), [], nil)
        }
        return (nil, [], "not_found")
    }

    private func fileSearchScore(_ url: URL, needle: String) -> Int {
        let name = url.lastPathComponent.lowercased()
        let stem = url.deletingPathExtension().lastPathComponent.lowercased()
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
        var score = 0
        if stem == needle || name == needle { score = 100 }
        else if stem.hasSuffix(" " + needle) { score = 85 }
        else if name.contains(needle) { score = 50 }
        if url.pathExtension.lowercased() == "pdf" && (needle == "resume" || needle == "cv" || needle == "pdf") {
            score += 14
        }
        if name.contains("flagship") && needle != "flagship" { score -= 12 }
        score -= min(stem.count, 24)
        return score
    }

    private func isDirectory(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
    }

    private func newestTextFile(in folder: URL) -> URL? {
        let fm = FileManager.default
        guard let children = try? fm.contentsOfDirectory(
            at: folder,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else {
            return nil
        }
        let files = children.filter { child in
            if isDirectory(child) { return false }
            if child.lastPathComponent.hasPrefix(".") { return false }
            if fileDenied(child) != nil { return false }
            let ext = child.pathExtension.lowercased()
            return ext.isEmpty || Self.textFileExtensions.contains(ext)
        }
        return files.max { lhs, rhs in
            let left = (try? lhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            let right = (try? rhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return left < right
        }
    }

    private func uniqueDefaultWriteURL(_ url: URL) -> URL {
        let fm = FileManager.default
        if url.lastPathComponent != "evie-note.txt" || !fm.fileExists(atPath: url.path) {
            return url
        }
        let folder = url.deletingLastPathComponent()
        let ext = url.pathExtension.isEmpty ? "txt" : url.pathExtension
        let stem = url.deletingPathExtension().lastPathComponent
        for index in 2..<80 {
            let candidate = folder.appendingPathComponent("\(stem)-\(index).\(ext)")
            if !fm.fileExists(atPath: candidate.path) {
                return candidate
            }
        }
        return folder.appendingPathComponent("\(stem)-\(Int(Date().timeIntervalSince1970)).\(ext)")
    }

    private func fileOp(_ arguments: [String: Any], requestId: String) -> [String: Any] {
        let action = (string(arguments, "action") ?? "").lowercased()
        let path = string(arguments, "path") ?? ""
        let query = string(arguments, "query") ?? ""
        let content = string(arguments, "content") ?? ""
        switch action {
        case "list":
            return fileList(path: path, query: query, requestId: requestId)
        case "read":
            let found = locateFile(path: path, query: query)
            guard var url = found.url else {
                return fileLocateFail(found, requestId: requestId)
            }
            if isDirectory(url) {
                guard let newest = newestTextFile(in: url) else {
                    return fail(
                        "not_found",
                        "I don't see a text file in \(url.lastPathComponent) yet.",
                        command: "file_op",
                        requestId: requestId
                    )
                }
                url = newest
            }
            return fileRead(url, requestId: requestId)
        case "write", "edit", "append":
            let found = locateFile(path: path, query: query)
            var url = found.url
            if url == nil, let created = expandFilePath(path.isEmpty ? query : path) {
                if let denied = fileDenied(created) {
                    return fail(denied, "I won't touch that path.", command: "file_op", requestId: requestId)
                }
                url = created
            }
            guard var dest = url else {
                return fileLocateFail(found, requestId: requestId)
            }
            if isDirectory(dest) {
                let name = query.isEmpty ? "evie-note.txt" : query
                dest = dest.appendingPathComponent(name)
            }
            if dest.pathExtension.isEmpty {
                dest = dest.appendingPathExtension("txt")
            }
            if !bool(arguments, "overwrite") {
                dest = uniqueDefaultWriteURL(dest)
            }
            var body = content
            if action == "append" {
                if dest.path.isEmpty == false, FileManager.default.fileExists(atPath: dest.path),
                   let existing = try? String(contentsOf: dest, encoding: .utf8)
                {
                    let prefix = existing.hasSuffix("\n") || existing.isEmpty ? existing : existing + "\n"
                    body = prefix + content
                }
            }
            return fileWrite(dest, content: body, requestId: requestId)
        case "open":
            let found = locateFile(path: path, query: query)
            guard var url = found.url else {
                return fileLocateFail(found, requestId: requestId)
            }
            if isDirectory(url) {
                guard let newest = newestTextFile(in: url) else {
                    return fail(
                        "not_found",
                        "I don't see a text file in \(url.lastPathComponent) yet.",
                        command: "file_op",
                        requestId: requestId
                    )
                }
                url = newest
            }
            if let denied = fileDenied(url) {
                return fail(denied, "I won't open that path.", command: "file_op", requestId: requestId)
            }
            let okOpen = NSWorkspace.shared.open(url)
            return ok(
                [
                    "ok": okOpen,
                    "executed": okOpen,
                    "verified": okOpen,
                    "action": "open",
                    "path": url.path,
                    "spoken": okOpen ? "Opened \(url.lastPathComponent)." : "I couldn't open \(url.lastPathComponent).",
                    "source": "mac_control",
                ],
                command: "file_op",
                requestId: requestId
            )
        default:
            return fail("unknown_action", "I can read, write, edit, list, or open local files.", command: "file_op", requestId: requestId)
        }
    }

    private func fileLocateFail(_ found: (url: URL?, matches: [URL], error: String?), requestId: String) -> [String: Any] {
        if found.error == "ambiguous" {
            let names = found.matches.prefix(8).map(\.lastPathComponent).joined(separator: ", ")
            return fail("ambiguous", "Which file do you mean? I found \(names).", command: "file_op", requestId: requestId)
        }
        if found.error == "path_denied" || found.error == "path_outside_allowed" {
            return fail(found.error ?? "path_denied", "I won't touch that path.", command: "file_op", requestId: requestId)
        }
        return fail(found.error ?? "not_found", "I couldn't find that file.", command: "file_op", requestId: requestId)
    }

    private func fileList(path: String, query: String, requestId: String) -> [String: Any] {
        var folder: URL?
        if let url = expandFilePath(path), FileManager.default.fileExists(atPath: url.path) {
            var isDir: ObjCBool = false
            FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
            folder = isDir.boolValue ? url : url.deletingLastPathComponent()
        }
        folder = folder ?? FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first
        guard let root = folder else {
            return fail("not_found", "I couldn't list that folder.", command: "file_op", requestId: requestId)
        }
        if let denied = fileDenied(root) {
            return fail(denied, "I won't list that folder.", command: "file_op", requestId: requestId)
        }
        let needle = query.lowercased()
        let children = (try? FileManager.default.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )) ?? []
        var names: [String] = []
        for child in children.sorted(by: { $0.lastPathComponent.lowercased() < $1.lastPathComponent.lowercased() }) {
            if !needle.isEmpty && !child.lastPathComponent.lowercased().contains(needle) { continue }
            names.append(child.lastPathComponent)
            if names.count >= Self.fileListCap { break }
        }
        let spoken = names.isEmpty ? "\(root.lastPathComponent) looks empty." : "On \(root.lastPathComponent): \(names.joined(separator: ", "))."
        return ok(
            [
                "ok": true,
                "executed": true,
                "verified": true,
                "action": "list",
                "path": root.path,
                "files": names,
                "count": names.count,
                "spoken": spoken,
                "source": "mac_control",
            ],
            command: "file_op",
            requestId: requestId
        )
    }

    private func fileRead(_ url: URL, requestId: String) -> [String: Any] {
        if let denied = fileDenied(url) {
            return fail(denied, "I won't read that path.", command: "file_op", requestId: requestId)
        }
        let ext = url.pathExtension.lowercased()
        if !ext.isEmpty && !Self.textFileExtensions.contains(ext) {
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber)?.intValue ?? 0
            return ok(
                [
                    "ok": true,
                    "executed": true,
                    "verified": true,
                    "action": "read",
                    "path": url.path,
                    "binary": true,
                    "size_bytes": size,
                    "spoken": "\(url.lastPathComponent) is not a text file I can read aloud.",
                    "source": "mac_control",
                ],
                command: "file_op",
                requestId: requestId
            )
        }
        guard let data = try? Data(contentsOf: url) else {
            return fail("not_a_file", "\(url.lastPathComponent) is not a file.", command: "file_op", requestId: requestId)
        }
        if data.count > Self.fileMaxBytes {
            return fail("too_large", "\(url.lastPathComponent) is larger than I will read.", command: "file_op", requestId: requestId)
        }
        let text = String(data: data, encoding: .utf8) ?? String(decoding: data, as: UTF8.self)
        let preview = text.trimmingCharacters(in: .whitespacesAndNewlines)
        var spoken = preview.isEmpty ? "\(url.lastPathComponent) is empty." : "The file says: \(preview)"
        if spoken.count > 420 {
            spoken = String(spoken.prefix(417)) + "..."
        }
        return ok(
            [
                "ok": true,
                "executed": true,
                "verified": true,
                "action": "read",
                "path": url.path,
                "content": text,
                "size_bytes": data.count,
                "spoken": spoken,
                "source": "mac_control",
            ],
            command: "file_op",
            requestId: requestId
        )
    }

    private func fileWrite(_ url: URL, content: String, requestId: String) -> [String: Any] {
        if let denied = fileDenied(url) {
            return fail(denied, "I won't write that path.", command: "file_op", requestId: requestId)
        }
        guard let data = content.data(using: .utf8), data.count <= Self.fileMaxBytes else {
            return fail("too_large", "That file is larger than I will write.", command: "file_op", requestId: requestId)
        }
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let tmp = url.appendingPathExtension("ev-tmp")
            try data.write(to: tmp, options: .atomic)
            if FileManager.default.fileExists(atPath: url.path) {
                _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
            } else {
                try FileManager.default.moveItem(at: tmp, to: url)
            }
            let check = try String(contentsOf: url, encoding: .utf8)
            let verified = check == content
            var payload: [String: Any] = [
                "ok": verified,
                "executed": true,
                "verified": verified,
                "action": "write",
                "path": url.path,
                "bytes": data.count,
                "spoken": "Wrote \(url.lastPathComponent).",
                "source": "mac_control",
            ]
            if !verified { payload["error"] = "verify_mismatch" }
            return ok(payload, command: "file_op", requestId: requestId)
        } catch {
            return fail("write_failed", "I couldn't write \(url.lastPathComponent).", command: "file_op", requestId: requestId)
        }
    }

    private func ok(_ data: [String: Any], command: String, requestId: String, ok: Bool = true) -> [String: Any] {
        var payload = data
        payload["ok"] = ok
        payload["command"] = command
        payload["request_id"] = requestId
        if payload["executed"] == nil {
            payload["executed"] = ok
        }
        return payload
    }

    private func fail(_ error: String, _ spoken: String, command: String, requestId: String) -> [String: Any] {
        [
            "ok": false,
            "executed": false,
            "verified": false,
            "error": error,
            "failure_class": error,
            "spoken": spoken,
            "command": command,
            "request_id": requestId,
        ]
    }

    private static func jpeg(_ image: CGImage) -> Data? {
        let maxWidth = 1280
        let scale = image.width > maxWidth ? CGFloat(maxWidth) / CGFloat(image.width) : 1
        let width = max(1, Int(CGFloat(image.width) * scale))
        let height = max(1, Int(CGFloat(image.height) * scale))
        let color = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: color,
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else { return nil }
        ctx.interpolationQuality = .medium
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let scaled = ctx.makeImage() else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, scaled, [kCGImageDestinationLossyCompressionQuality: 0.62] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
    }

    private static func shortRole(_ role: String) -> String {
        switch role {
        case "AXButton": return "button"
        case "AXCheckBox": return "checkbox"
        case "AXRadioButton": return "radio"
        case "AXTextField", "AXSearchField": return "field"
        case "AXTextArea": return "textarea"
        case "AXPopUpButton": return "popup"
        case "AXMenuItem": return "menu"
        case "AXMenuButton": return "menu-button"
        case "AXTab", "AXTabGroup": return "tab"
        case "AXLink": return "link"
        case "AXSlider": return "slider"
        case "AXStaticText": return "text"
        case "AXWindow": return "window"
        case "AXSheet", "AXDialog": return "dialog"
        case "AXScrollArea": return "scroll"
        case "AXWebArea": return "web"
        case "AXRow": return "row"
        case "AXCell": return "cell"
        default:
            return role.hasPrefix("AX") ? String(role.dropFirst(2)).lowercased() : role.lowercased()
        }
    }

    private static func keyCode(_ name: String) -> CGKeyCode? {
        switch name {
        case "k": return CGKeyCode(kVK_ANSI_K)
        case "a": return CGKeyCode(kVK_ANSI_A)
        case "b": return CGKeyCode(kVK_ANSI_B)
        case "c": return CGKeyCode(kVK_ANSI_C)
        case "f": return CGKeyCode(kVK_ANSI_F)
        case "l": return CGKeyCode(kVK_ANSI_L)
        case "n": return CGKeyCode(kVK_ANSI_N)
        case "o": return CGKeyCode(kVK_ANSI_O)
        case "q": return CGKeyCode(kVK_ANSI_Q)
        case "r": return CGKeyCode(kVK_ANSI_R)
        case "s": return CGKeyCode(kVK_ANSI_S)
        case "t": return CGKeyCode(kVK_ANSI_T)
        case "v": return CGKeyCode(kVK_ANSI_V)
        case "w": return CGKeyCode(kVK_ANSI_W)
        case "x": return CGKeyCode(kVK_ANSI_X)
        case "z": return CGKeyCode(kVK_ANSI_Z)
        case "return", "enter": return CGKeyCode(kVK_Return)
        case "escape", "esc": return CGKeyCode(kVK_Escape)
        case "tab": return CGKeyCode(kVK_Tab)
        case "space": return CGKeyCode(kVK_Space)
        case "delete": return CGKeyCode(kVK_Delete)
        case "[": return CGKeyCode(kVK_ANSI_LeftBracket)
        case "]": return CGKeyCode(kVK_ANSI_RightBracket)
        case "down": return CGKeyCode(kVK_DownArrow)
        case "up": return CGKeyCode(kVK_UpArrow)
        case "left": return CGKeyCode(kVK_LeftArrow)
        case "right": return CGKeyCode(kVK_RightArrow)
        case "0": return CGKeyCode(kVK_ANSI_0)
        case "1": return CGKeyCode(kVK_ANSI_1)
        case "2": return CGKeyCode(kVK_ANSI_2)
        case "3": return CGKeyCode(kVK_ANSI_3)
        case "4": return CGKeyCode(kVK_ANSI_4)
        case "5": return CGKeyCode(kVK_ANSI_5)
        case "6": return CGKeyCode(kVK_ANSI_6)
        case "7": return CGKeyCode(kVK_ANSI_7)
        case "8": return CGKeyCode(kVK_ANSI_8)
        case "9": return CGKeyCode(kVK_ANSI_9)
        case "*": return CGKeyCode(kVK_ANSI_KeypadMultiply)
        case "+": return CGKeyCode(kVK_ANSI_KeypadPlus)
        case "-": return CGKeyCode(kVK_ANSI_Minus)
        case "/": return CGKeyCode(kVK_ANSI_Slash)
        case "=": return CGKeyCode(kVK_ANSI_KeypadEnter)
        case ".": return CGKeyCode(kVK_ANSI_Period)
        default: return nil
        }
    }

    private static let interactive: Set<String> = [
        "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton",
        "AXMenuItem", "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField",
        "AXLink", "AXTab", "AXTabGroup", "AXSlider", "AXIncrementor",
        "AXDisclosureTriangle", "AXRow", "AXCell", "AXOutline", "AXTable", "AXList",
        "AXScrollBar", "AXScrollArea", "AXWebArea", "AXSheet", "AXDialog", "AXWindow",
    ]

    private static let skipRoles: Set<String> = [
        "AXImage", "AXBusyIndicator", "AXLayoutArea", "AXColorWell",
    ]

    private static func priority(_ role: String) -> Int {
        switch role {
        case "AXTextArea": return 90
        case "AXTextField", "AXSearchField": return 80
        case "AXButton", "AXCheckBox", "AXLink": return 70
        case "AXPopUpButton", "AXMenuItem", "AXTab": return 60
        case "AXSheet", "AXDialog": return 85
        case "AXWindow": return 40
        case "AXStaticText": return 20
        default: return 10
        }
    }

    private static let protected: Set<String> = [
        "com.apple.finder",
        "com.apple.loginwindow",
        "com.ev.suit",
    ]

    private static let known: [String: String] = [
        "safari": "com.apple.Safari",
        "messages": "com.apple.MobileSMS",
        "mail": "com.apple.mail",
        "calendar": "com.apple.iCal",
        "finder": "com.apple.finder",
        "notes": "com.apple.Notes",
        "music": "com.apple.Music",
        "photos": "com.apple.Photos",
        "maps": "com.apple.Maps",
        "facetime": "com.apple.FaceTime",
        "reminders": "com.apple.reminders",
        "settings": "com.apple.systempreferences",
        "terminal": "com.apple.Terminal",
        "textedit": "com.apple.TextEdit",
        "calculator": "com.apple.calculator",
        "chrome": "com.google.Chrome",
        "arc": "company.thebrowser.Browser",
        "slack": "com.tinyspeck.slackmacgap",
        "spotify": "com.spotify.client",
        "cursor": "com.todesktop.230313mzl4w4u92",
        "vscode": "com.microsoft.VSCode",
        "code": "com.microsoft.VSCode",
    ]

    private static let aliases: [String: String] = [
        "google chrome": "chrome",
        "imessage": "messages",
        "system settings": "settings",
        "system preferences": "settings",
        "text edit": "textedit",
        "calc": "calculator",
        "vs code": "vscode",
        "visual studio code": "vscode",
        "browser": "safari",
        "bluetooth": "bluetooth settings",
        "bluetooth settings": "bluetooth settings",
        "wifi": "wifi",
        "wi-fi": "wifi",
        "displays": "displays",
        "display": "displays",
        "display settings": "displays",
        "downloads": "downloads",
    ]

    private static let settingsPanes: [String: String] = [
        "bluetooth settings": "x-apple.systempreferences:com.apple.BluetoothSettings",
        "wifi": "x-apple.systempreferences:com.apple.wifi-settings-extension",
        "displays": "x-apple.systempreferences:com.apple.Displays-Settings.extension",
    ]
}
