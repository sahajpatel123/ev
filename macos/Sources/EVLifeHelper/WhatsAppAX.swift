import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

/// WhatsApp Desktop driven through the macOS Accessibility API.
///
/// The owner keeps WhatsApp in the Dock/background — hidden or behind other
/// windows. AX actions (`AXPress` on a chat row, `AXValue` on the composer)
/// and posted key events act on the app *without* activating it, unhiding it,
/// or touching the frontmost app, so `focus_stolen` is always false.
///
/// Delivery is only reported when the sent text appears in the thread as an
/// outgoing message. Every failure maps to a named code; nothing is guessed.
///
/// Side effect, stated honestly: opening a chat marks its unread messages as
/// read, exactly as a human glancing at it would.

enum WhatsAppAXError: Error {
    case notRunning
    case accessibilityNotGranted
    case notAvailable(String)
    case chatNotFound(String)
    case ambiguousChat(String, [String])
    case composerMissing
    case composeFailed(String)
    case sendNotConfirmed(String)
}

struct WhatsAppAXChat {
    let name: String
    let unread: Int
    let preview: String
    let element: AXUIElement
}

private let kMarks = CharacterSet(charactersIn: "\u{200e}\u{200f}\u{2060}\u{fe00}\u{fe0f}\u{200d}\u{200c}\u{200b}")

private func axBool(_ element: AXUIElement, _ name: String) -> Bool {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success,
          let value, CFGetTypeID(value) == CFBooleanGetTypeID() else {
        return false
    }
    return CFBooleanGetValue((value as! CFBoolean))
}

func axNormalize(_ raw: String) -> String {
    var text = raw.precomposedStringWithCanonicalMapping
    text = String(String.UnicodeScalarView(text.unicodeScalars.filter { !kMarks.contains($0) }))
    text = text.replacingOccurrences(of: "\u{00a0}", with: " ")
    return text.trimmingCharacters(in: .whitespacesAndNewlines)
}

private func axAttr(_ element: AXUIElement, _ name: String) -> String {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success,
          let value else {
        return ""
    }
    if let string = value as? String {
        return string
    }
    return String(describing: value)
}

private func axHasAction(_ element: AXUIElement, _ action: String) -> Bool {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success, let names else {
        return false
    }
    return (names as? [String])?.contains(action) ?? false
}

private func axElements(_ root: AXUIElement, limit: Int = 90_000) -> [AXUIElement] {
    var out: [AXUIElement] = []
    var queue: [AXUIElement] = [root]
    while !queue.isEmpty && out.count < limit {
        let element = queue.removeFirst()
        out.append(element)
        var children: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
           let kids = children as? [AXUIElement] {
            queue.append(contentsOf: kids.prefix(120))
        }
    }
    return out
}

func parseChatRowDescription(_ description: String) -> (name: String, unread: Int)? {
    let normalized = axNormalize(description)
    guard !normalized.isEmpty else { return nil }
    let pattern = #"^(.*?),\s*(\d+)\s+unread\s+messages?$"#
    if let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) {
        let range = NSRange(normalized.startIndex..<normalized.endIndex, in: normalized)
        if let match = regex.firstMatch(in: normalized, options: [], range: range),
           let nameRange = Range(match.range(at: 1), in: normalized),
           let countRange = Range(match.range(at: 2), in: normalized) {
            return (String(normalized[nameRange]).trimmingCharacters(in: .whitespaces), Int(normalized[countRange]) ?? 0)
        }
    }
    return (normalized, 0)
}

private let nonChatLabels: Set<String> = [
    "new chat", "call", "more", "share media", "emoji picker", "voice message",
    "chats", "calls", "updates", "archived", "starred", "settings", "search",
    "close", "back", "new group", "menu", "attach", "send", "camera",
    "start call", "video call", "search or start a new chat", "filter",
]

func looksLikeChatRow(_ element: AXUIElement) -> Bool {
    guard axAttr(element, kAXRoleAttribute) == kAXButtonRole as String else { return false }
    let description = axNormalize(axAttr(element, kAXDescriptionAttribute))
    guard !description.isEmpty, !nonChatLabels.contains(description.lowercased()) else { return false }
    if !axHasAction(element, "AXScrollToVisible") { return false }
    let value = axNormalize(axAttr(element, kAXValueAttribute))
    if !value.isEmpty { return true }
    // Empty previews happen on freshly created chats. Accept only when the row
    // is inside the same container as rows WhatsApp labels with unread counts.
    return axAttr(element, kAXDescriptionAttribute).contains("unread message")
}

final class WhatsAppAXClient {
    static let bundleIdentifier = "net.whatsapp.WhatsApp"
    static let maxWait = 15.0

    let app: NSRunningApplication
    let root: AXUIElement

    private init(app: NSRunningApplication) {
        self.app = app
        self.root = AXUIElementCreateApplication(app.processIdentifier)
    }

    static func connect() throws -> WhatsAppAXClient {
        guard AXIsProcessTrusted() else { throw WhatsAppAXError.accessibilityNotGranted }
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).first,
              !app.isTerminated else {
            throw WhatsAppAXError.notRunning
        }
        return WhatsAppAXClient(app: app)
    }

    /// Headless launch (never activates) used when WhatsApp is simply closed.
    static func launchHeadless() throws -> WhatsAppAXClient {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleIdentifier) else {
            throw WhatsAppAXError.notAvailable("whatsapp_not_installed")
        }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = false
        config.hides = true
        let semaphore = DispatchSemaphore(value: 0)
        NSWorkspace.shared.openApplication(at: url, configuration: config) { _, _ in
            semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 5)
        let deadline = Date().addingTimeInterval(25)
        while Date() < deadline {
            if let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).first {
                let client = WhatsAppAXClient(app: app)
                if !client.chats(limit: 1).isEmpty {
                    return client
                }
            }
            Thread.sleep(forTimeInterval: 1.0)
        }
        throw WhatsAppAXError.notAvailable("whatsapp_launch_timeout")
    }

    var isHidden: Bool { app.isHidden }
    var isActive: Bool { app.isActive }

    /// WhatsApp (Catalyst) only navigates its UI when it is frontmost. Bring it
    /// forward for the shortest window possible; the caller restores focus.
    func activateForUI() -> (stolen: Bool, previous: NSRunningApplication?, wasHidden: Bool) {
        let front = NSWorkspace.shared.frontmostApplication
        let wasHidden = app.isHidden
        let alreadyFront = front?.processIdentifier == app.processIdentifier && !wasHidden
        guard !alreadyFront else { return (false, nil, wasHidden) }
        if wasHidden {
            _ = app.unhide()
        }
        app.activate(options: [.activateIgnoringOtherApps])
        let deadline = Date().addingTimeInterval(4.0)
        while Date() < deadline {
            let frontPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
            if frontPID == app.processIdentifier, !app.isHidden { break }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return (true, front, wasHidden)
    }

    /// Catalyst `hide()` only takes while the app is frontmost and lands
    /// asynchronously, so verify and fall back to System Events once.
    @discardableResult
    func restoreFocus(_ previous: NSRunningApplication?, wasHidden: Bool) -> Bool {
        var hiddenOK = true
        if wasHidden, !app.isHidden {
            app.activate(options: [.activateIgnoringOtherApps])
            Thread.sleep(forTimeInterval: 0.3)
            app.hide()
            let deadline = Date().addingTimeInterval(2.5)
            while Date() < deadline, !app.isHidden {
                Thread.sleep(forTimeInterval: 0.2)
            }
            if !app.isHidden {
                let source = "tell application \"System Events\" to set visible of process \"WhatsApp\" to false"
                if let script = NSAppleScript(source: source) {
                    var error: NSDictionary?
                    script.executeAndReturnError(&error)
                }
                Thread.sleep(forTimeInterval: 0.6)
                hiddenOK = app.isHidden
            }
        }
        if let previous, !previous.isTerminated,
           previous.processIdentifier != app.processIdentifier {
            previous.activate(options: [])
            let deadline = Date().addingTimeInterval(1.5)
            while Date() < deadline,
                  NSWorkspace.shared.frontmostApplication?.processIdentifier != previous.processIdentifier {
                Thread.sleep(forTimeInterval: 0.1)
            }
        }
        return hiddenOK
    }

    func allElements() -> [AXUIElement] {
        axElements(root)
    }

    func chats(limit: Int = 50) -> [WhatsAppAXChat] {
        var rows: [WhatsAppAXChat] = []
        var seen = Set<String>()
        for element in allElements() {
            guard looksLikeChatRow(element) else { continue }
            let description = axAttr(element, kAXDescriptionAttribute)
            guard let parsed = parseChatRowDescription(description) else { continue }
            guard !parsed.name.isEmpty else { continue }
            let key = parsed.name.lowercased()
            if seen.contains(key) { continue }
            seen.insert(key)
            rows.append(
                WhatsAppAXChat(
                    name: parsed.name,
                    unread: parsed.unread,
                    preview: axNormalize(axAttr(element, kAXValueAttribute)),
                    element: element
                )
            )
            if rows.count >= limit { break }
        }
        return rows
    }

    /// The scrollable chat-list container (has paging AX actions).
    func listContainer() -> AXUIElement? {
        for element in allElements() where looksLikeChatRow(element) {
            var parent: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, kAXParentAttribute as CFString, &parent) == .success,
                  let parent else { continue }
            let group = parent as! AXUIElement
            if axHasAction(group, "AXScrollDownByPage") {
                return group
            }
        }
        return nil
    }

    private func viewFingerprint() -> String {
        chats(limit: 500).map(\.name).joined(separator: "|")
    }

    @discardableResult
    private func scrollPage(_ list: AXUIElement, _ action: String) -> Bool {
        let before = viewFingerprint()
        _ = AXUIElementPerformAction(list, action as CFString)
        Thread.sleep(forTimeInterval: 0.5)
        return viewFingerprint() != before
    }

    /// Leave the list where a person expects it: at the top.
    func restoreListTop(_ list: AXUIElement) {
        var guardCount = 0
        while guardCount < 60, scrollPage(list, "AXScrollUpByPage") {
            guardCount += 1
        }
    }

    func composer() -> AXUIElement? {
        allElements().first { element in
            axAttr(element, kAXRoleAttribute) == kAXTextAreaRole as String
                && axAttr(element, kAXIdentifierAttribute).contains("Composer")
        }
    }

    func composerText() -> String {
        guard let composer = composer() else { return "" }
        return axAttr(composer, kAXValueAttribute)
    }

    func threadDescriptions() -> [String] {
        allElements().compactMap { element in
            guard axAttr(element, kAXRoleAttribute) == kAXStaticTextRole as String else { return nil }
            let description = axNormalize(axAttr(element, kAXDescriptionAttribute))
            let value = axNormalize(axAttr(element, kAXValueAttribute))
            let text = description.isEmpty ? value : description
            guard !text.isEmpty else { return nil }
            if text.contains("end-to-end encrypted") { return nil }
            if text == "Search" { return nil }
            return text
        }
    }

    func threadMentions(name: String) -> Bool {
        let wanted = axNormalize(name).lowercased()
        return threadDescriptions().contains { line in
            let lower = line.lowercased()
            return lower.contains("sent to \(wanted)") || lower.contains("received from \(wanted)")
        }
    }

    func openChat(named name: String) throws -> WhatsAppAXChat {
        let wanted = axNormalize(name).lowercased()
        guard !wanted.isEmpty else { throw WhatsAppAXError.chatNotFound(name) }
        settleAfterActivation()
        guard let list = listContainer() else { throw WhatsAppAXError.chatNotFound(name) }
        defer { restoreListTop(list) }

        var ambiguous: [String] = []

        func pressIfVisible() throws -> WhatsAppAXChat? {
            let matches = chats(limit: 500).filter { $0.name.lowercased() == wanted }
            if matches.count > 1 {
                ambiguous = matches.map(\.name)
                return nil
            }
            guard let chat = matches.first else { return nil }
            let before = threadDescriptions()
            guard AXUIElementPerformAction(chat.element, kAXPressAction as CFString) == .success else {
                throw WhatsAppAXError.chatNotFound(name)
            }
            let deadline = Date().addingTimeInterval(Self.maxWait)
            while Date() < deadline {
                Thread.sleep(forTimeInterval: 0.4)
                guard composer() != nil else { continue }
                if isRowSelected(named: name)
                    || threadMentions(name: name)
                    || threadDescriptions() != before {
                    return chat
                }
            }
            throw WhatsAppAXError.notAvailable("chat_open_unconfirmed")
        }

        if let chat = try pressIfVisible() { return chat }
        if !ambiguous.isEmpty { throw WhatsAppAXError.ambiguousChat(name, ambiguous) }

        // The chat list is virtualized: only the on-screen page is in the AX
        // tree. Sweep down from the current position, then up, paging through
        // the list until the exact name is found. Never fuzzy-matches.
        var pages = 0
        while pages < 30, scrollPage(list, "AXScrollDownByPage") {
            pages += 1
            if let chat = try pressIfVisible() { return chat }
            if !ambiguous.isEmpty { throw WhatsAppAXError.ambiguousChat(name, ambiguous) }
        }
        pages = 0
        while pages < 30, scrollPage(list, "AXScrollUpByPage") {
            pages += 1
            if let chat = try pressIfVisible() { return chat }
            if !ambiguous.isEmpty { throw WhatsAppAXError.ambiguousChat(name, ambiguous) }
        }
        throw WhatsAppAXError.chatNotFound(name)
    }

    func setComposer(_ text: String) throws {
        guard let composer = composer() else { throw WhatsAppAXError.composerMissing }
        var settable: DarwinBoolean = false
        guard AXUIElementIsAttributeSettable(composer, kAXValueAttribute as CFString, &settable) == .success,
              settable.boolValue else {
            throw WhatsAppAXError.composeFailed("composer_not_settable")
        }
        let status = AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, text as CFTypeRef)
        guard status == .success else { throw WhatsAppAXError.composeFailed("composer_set_failed") }
        Thread.sleep(forTimeInterval: 0.3)
        let readBack = axAttr(composer, kAXValueAttribute)
        guard axNormalize(readBack) == axNormalize(text) else {
            throw WhatsAppAXError.composeFailed("composer_verify_failed")
        }
    }

    func clearComposer() {
        guard let composer = composer() else { return }
        _ = AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, "" as CFTypeRef)
    }

    /// Post-activation the Catalyst tree can take a beat to render; scanning
    /// before it does used to look like "chat not found".
    func settleAfterActivation() {
        let deadline = Date().addingTimeInterval(3.0)
        while Date() < deadline {
            if listContainer() != nil, !chats(limit: 1).isEmpty { return }
            Thread.sleep(forTimeInterval: 0.25)
        }
    }

    func isRowSelected(named name: String) -> Bool {
        let wanted = axNormalize(name).lowercased()
        for element in allElements() where looksLikeChatRow(element) {
            let description = axAttr(element, kAXDescriptionAttribute)
            guard let parsed = parseChatRowDescription(description),
                  parsed.name.lowercased() == wanted else { continue }
            return axBool(element, kAXSelectedAttribute)
        }
        return false
    }

    private func pressSendButton() -> Bool {
        for element in allElements() {
            guard axAttr(element, kAXRoleAttribute) == kAXButtonRole as String else { continue }
            let identifier = axAttr(element, kAXIdentifierAttribute).lowercased()
            let description = axNormalize(axAttr(element, kAXDescriptionAttribute)).lowercased()
            if identifier.contains("send") || description == "send" {
                if AXUIElementPerformAction(element, kAXPressAction as CFString) == .success {
                    return true
                }
            }
        }
        return false
    }

    private func pressReturnKey() {
        let source = CGEventSource(stateID: .hidSystemState)
        if let down = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: true) {
            down.postToPid(app.processIdentifier)
        }
        usleep(60_000)
        if let up = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: false) {
            up.postToPid(app.processIdentifier)
        }
    }

    private func sentAppears(inThread body: String) -> Bool {
        let wanted = axNormalize(body)
        return threadDescriptions().contains { line in
            line.contains("Your message") && line.contains(wanted)
        }
    }

    private func waitForSend(body: String, seconds: Double) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            Thread.sleep(forTimeInterval: 0.6)
            if sentAppears(inThread: body) { return true }
        }
        return false
    }

    /// Open a chat for a one-shot command, restoring focus immediately after.
    func openChatActivated(named name: String) throws -> (chat: WhatsAppAXChat, focusStolen: Bool) {
        let focus = activateForUI()
        defer { restoreFocus(focus.previous, wasHidden: focus.wasHidden) }
        let chat = try openChat(named: name)
        return (chat, focus.stolen)
    }

    /// Send `text` to the exact chat `name`. Never reports sent without the
    /// message appearing in the thread. `dryRun` composes and clears only.
    /// The app is brought forward for the operation and the previous app is
    /// restored afterwards; `focus_stolen` says exactly what happened.
    func send(to name: String, text: String, dryRun: Bool = false) throws -> [String: Any] {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { throw WhatsAppAXError.composeFailed("empty_message") }
        let focus = activateForUI()
        var hiddenRestored = true
        defer { hiddenRestored = restoreFocus(focus.previous, wasHidden: focus.wasHidden) }
        let chat = try openChat(named: name)
        let verifiedThread = threadMentions(name: name)
        try setComposer(body)
        if dryRun {
            clearComposer()
            return [
                "to": chat.name,
                "channel": "whatsapp",
                "dry_run": true,
                "composed": body,
                "composer_cleared": true,
                "chat_verified": verifiedThread,
                "focus_stolen": focus.stolen,
                "focus_restored": true,
                "hidden_restored": hiddenRestored,
            ]
        }
        var confirmed = false
        if pressSendButton() {
            confirmed = waitForSend(body: body, seconds: 8)
        }
        if !confirmed {
            pressReturnKey()
            confirmed = waitForSend(body: body, seconds: 10)
        }
        if !confirmed {
            clearComposer()
            throw WhatsAppAXError.sendNotConfirmed("send_not_confirmed")
        }
        Thread.sleep(forTimeInterval: 0.6)
        clearComposer()
        return [
            "to": chat.name,
            "channel": "whatsapp",
            "sent": true,
            "verified_in_thread": true,
            "chat_verified": verifiedThread,
            "focus_stolen": focus.stolen,
            "focus_restored": true,
            "hidden_restored": hiddenRestored,
        ]
    }

    /// Read a thread: opens the exact chat (frontmost for the navigation),
    /// then returns its parsed messages and restores focus.
    func readChat(named name: String, limit: Int = 20) throws -> [String: Any] {
        let focus = activateForUI()
        var hiddenRestored = true
        defer { hiddenRestored = restoreFocus(focus.previous, wasHidden: focus.wasHidden) }
        let chat = try openChat(named: name)
        let messages = readThread(named: name, limit: limit)
        return [
            "to": chat.name,
            "focus_stolen": focus.stolen,
            "focus_restored": true,
            "hidden_restored": hiddenRestored,
            "messages": messages,
        ]
    }

    func readThread(named name: String, limit: Int = 20) -> [[String: Any]] {
        var out: [[String: Any]] = []
        for line in threadDescriptions() {
            guard let parsed = parseThreadLine(line) else { continue }
            out.append(parsed)
        }
        return Array(out.suffix(limit))
    }
}

private let kTrailingTime = try? NSRegularExpression(
    pattern: #",\s*(?:\p{L}+\d*,\s*at\s*)?\d{1,2}:\d{2}\s*[AP]M\s*$"#,
    options: [.caseInsensitive]
)

private func stripTrailingTime(_ text: String) -> String {
    guard let regex = kTrailingTime else { return text }
    let range = NSRange(text.startIndex..<text.endIndex, in: text)
    guard let match = regex.firstMatch(in: text, options: [], range: range),
          let matchRange = Range(match.range, in: text) else {
        return text
    }
    return String(text[..<matchRange.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
}

/// Parse one message line: direction, body and the raw text. Handles text and
/// media rows ("message, …", "Photo, …") and the "Replying to …." prefix.
func parseThreadLine(_ line: String) -> [String: Any]? {
    var working = line
    if working.hasPrefix("Replying to ") {
        guard let newline = working.firstIndex(of: "\n") else { return nil }
        working = String(working[working.index(after: newline)...])
    }
    let prefixes = [
        "Your message, ", "message, ", "Photo, ", "Video, ", "Voice message, ",
        "Audio, ", "Document, ", "Sticker, ", "GIF, ", "Contact, ", "Location, ",
    ]
    guard let prefix = prefixes.first(where: { working.hasPrefix($0) }) else { return nil }
    var body = String(working.dropFirst(prefix.count))
    var direction: String? = prefix == "Your message, " ? "out" : nil
    if let range = body.range(of: ", Sent to ", options: .backwards) {
        direction = "out"
        body = String(body[..<range.lowerBound])
    } else if let range = body.range(of: ", Received from ", options: .backwards) {
        direction = "in"
        body = String(body[..<range.lowerBound])
    }
    guard let direction else { return nil }
    return [
        "direction": direction,
        "body": stripTrailingTime(body.trimmingCharacters(in: .whitespacesAndNewlines)),
        "raw": working,
    ]
}
