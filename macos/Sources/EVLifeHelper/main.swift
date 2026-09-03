import AppKit
import Contacts
import CoreServices
import Foundation

/// EVLifeHelper — JSON stdout contract for Apple-surface life access.
///
/// Exit codes (contract, stable):
///   0 = ok
///   3 = permission denied (TCC not granted; never fake success)
///   4 = not available (feature/OS/app missing)
///   5 = bad arguments
///   1 = generic failure
///
/// Output is always one JSON object on stdout:
///   success: {"ok":true,"data":{...}}
///   failure: {"ok":false,"error":{"code":"...","message":"..."}}

enum LifeExit: Int32 {
    case ok = 0
    case failed = 1
    case permissionDenied = 3
    case notAvailable = 4
    case badArguments = 5
}

enum LifeError: Error {
    case failed(String)
}

func printJSON(_ object: [String: Any]) {
    guard
        let data = try? JSONSerialization.data(
            withJSONObject: object,
            options: [.prettyPrinted, .sortedKeys]
        ),
        let string = String(data: data, encoding: .utf8)
    else {
        print(#"{"ok":false,"error":{"code":"failed","message":"json encoding failed"}}"#)
        return
    }
    print(string)
}

func success(_ data: [String: Any]) -> Never {
    printJSON(["ok": true, "data": data])
    exit(LifeExit.ok.rawValue)
}

func fail(_ code: LifeExit, _ reason: String, _ message: String) -> Never {
    printJSON(["ok": false, "error": ["code": reason, "message": message]])
    exit(code.rawValue)
}

func argumentValue(_ name: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: name), index + 1 < CommandLine.arguments.count else {
        return nil
    }
    return CommandLine.arguments[index + 1]
}

func appleScriptEscape(_ string: String) -> String {
    string
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: "\\n")
}

func runAppleScript(_ source: String) throws -> String {
    var error: NSDictionary?
    guard let script = NSAppleScript(source: source) else {
        throw LifeError.failed("could not compile AppleScript")
    }
    let result = script.executeAndReturnError(&error)
    if let error {
        if let number = error[NSAppleScript.errorNumber] as? Int,
           number == -1743 {
            fail(
                .permissionDenied,
                "permission_denied",
                "Automation permission denied for EV. Grant it in System Settings → Privacy & Security → Automation."
            )
        }
        throw LifeError.failed(error[NSAppleScript.errorMessage] as? String ?? "AppleScript execution failed")
    }
    return result.stringValue ?? ""
}

/// Launch an app without activating it or stealing focus.
func launchBundleHeadless(_ bundleID: String) {
    guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
        return
    }
    let config = NSWorkspace.OpenConfiguration()
    config.activates = false
    config.hides = true
    config.addsToRecentItems = false
    let sema = DispatchSemaphore(value: 0)
    NSWorkspace.shared.openApplication(at: url, configuration: config) { _, _ in
        sema.signal()
    }
    _ = sema.wait(timeout: .now() + 1.5)
}

func hideProcess(_ name: String) {
    let script = """
    tell application "System Events"
        if exists process "\(appleScriptEscape(name))" then
            set visible of process "\(appleScriptEscape(name))" to false
        end if
    end tell
    """
    _ = try? runAppleScript(script)
}

func openURLHeadless(_ url: URL) -> Bool {
    let config = NSWorkspace.OpenConfiguration()
    config.activates = false
    config.hides = true
    config.addsToRecentItems = false
    var opened = false
    let sema = DispatchSemaphore(value: 0)
    NSWorkspace.shared.open(url, configuration: config) { _, err in
        opened = err == nil
        sema.signal()
    }
    _ = sema.wait(timeout: .now() + 2.0)
    return opened
}

func compileAppleScript(_ source: String) throws -> Bool {
    var error: NSDictionary?
    guard let script = NSAppleScript(source: source) else {
        throw LifeError.failed("could not compile AppleScript")
    }
    guard script.compileAndReturnError(&error) else {
        throw LifeError.failed(
            error?[NSAppleScript.errorMessage] as? String ?? "AppleScript compilation failed"
        )
    }
    return true
}

/// Per-target Automation permission check without prompting. Returns nil when
/// the target is not running or the OS has not decided yet.
func automationGranted(for bundleID: String) -> Bool? {
    guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
        return nil
    }
    var pid = app.processIdentifier
    var descriptor = AEAddressDesc()
    let status = AECreateDesc(
        typeKernelProcessID,
        &pid,
        MemoryLayout<pid_t>.size,
        &descriptor
    )
    guard status == OSStatus(noErr) else { return nil }
    defer { AEDisposeDesc(&descriptor) }
    let permission = AEDeterminePermissionToAutomateTarget(
        &descriptor,
        typeWildCard,
        typeWildCard,
        false
    )
    switch permission {
    case OSStatus(noErr): return true
    case OSStatus(errAEEventNotPermitted): return false
    default: return nil
    }
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    printJSON([
        "ok": false,
        "error": [
            "code": "bad_arguments",
            "message": """
            usage: EVLifeHelper <command> [args]
            commands:
              contacts.list
              contacts.resolve --query <name|phone|email>
              contacts.create --name <name> [--phone <phone>] [--email <email>] [--company <company>]
              contacts.update [--id <id>] [--query <name>] [--name <name>] [--phone <phone>] [--email <email>] [--company <company>]
              messages.list [--limit N]
              messages.send --to <buddy> --text <message> [--dry-run]
              mail.list [--limit N]
              mail.send --to <email> --subject <subject> --body <body> [--dry-run]
              call.place --destination <number> [--kind tel|facetime]
              call.check --destination <number> [--kind tel|facetime]
              apps.frontmost
              apps.list [--query <name>] [--running true]
              apps.activate --bundle-id <id> [--name <name>]
              apps.quit --bundle-id <id>
              open.url --url <https-url>
            """,
        ],
    ])
    exit(LifeExit.badArguments.rawValue)
}

let command = arguments[1]

switch command {

// MARK: - Contacts

case "contacts.list":
    let contacts = try? fetchContacts(query: nil)
    guard let contacts else {
        fail(.failed, "failed", "contact fetch failed")
    }
    success(["contacts": contacts])

case "contacts.resolve":
    guard let query = argumentValue("--query"), !query.isEmpty else {
        fail(.badArguments, "bad_arguments", "contacts.resolve requires --query")
    }
    let matches = (try? fetchContacts(query: query)) ?? []
    success(["query": query, "matches": Array(matches.prefix(10))])

case "contacts.create":
    guard let name = argumentValue("--name"), !name.isEmpty else {
        fail(.badArguments, "bad_arguments", "contacts.create requires --name")
    }
    let phone = argumentValue("--phone")
    let email = argumentValue("--email")
    let company = argumentValue("--company")
    do {
        let created = try createContact(name: name, phone: phone, email: email, company: company)
        success(created)
    } catch {
        fail(.failed, "failed", "contacts.create failed: \(error)")
    }

case "contacts.update":
    let contactId = argumentValue("--id")
    let query = argumentValue("--query")
    if (contactId == nil || contactId!.isEmpty) && (query == nil || query!.isEmpty) {
        fail(.badArguments, "bad_arguments", "contacts.update requires --id or --query")
    }
    let name = argumentValue("--name")
    let phone = argumentValue("--phone")
    let email = argumentValue("--email")
    let company = argumentValue("--company")
    do {
        let updated = try updateContact(identifier: contactId, query: query, name: name, phone: phone, email: email, company: company)
        success(updated)
    } catch {
        fail(.failed, "failed", "contacts.update failed: \(error)")
    }

// MARK: - Messages

case "messages.list":
    let limit = Int(argumentValue("--limit") ?? "20") ?? 20
    let messages = try? listMessages(limit: limit)
    guard let messages else {
        fail(.failed, "failed", "messages.list failed (Full Disk Access or Messages DB unavailable)")
    }
    success(["messages": messages])

case "messages.send":
    guard let recipient = argumentValue("--to"), !recipient.isEmpty else {
        fail(.badArguments, "bad_arguments", "messages.send requires --to")
    }
    guard let text = argumentValue("--text"), !text.isEmpty else {
        fail(.badArguments, "bad_arguments", "messages.send requires --text")
    }
    do {
        let script = """
        tell application "Messages" to launch
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            set targetBuddy to buddy "\(appleScriptEscape(recipient))" of targetService
            send "\(appleScriptEscape(text))" to targetBuddy
        end tell
        """
        if arguments.contains("--dry-run") {
            guard automationGranted(for: "com.apple.MobileSMS") == true else {
                fail(
                    .permissionDenied,
                    "permission_denied",
                    "Automation for Messages is not granted; a real send would fail. Dry-run aborted."
                )
            }
            _ = try compileAppleScript(script)
            success(["to": recipient, "dry_run": true, "compiled": true, "headless": true])
        }
        launchBundleHeadless("com.apple.MobileSMS")
        _ = try runAppleScript(script)
        hideProcess("Messages")
        success(["to": recipient, "sent": true, "headless": true, "focus_stolen": false])
    } catch {
        fail(.failed, "failed", "messages.send failed: \(error)")
    }

// MARK: - Mail

case "mail.list":
    let limit = Int(argumentValue("--limit") ?? "10") ?? 10
    do {
        let script = """
        tell application "Mail" to launch
        tell application "Mail"
            set n to count of messages of inbox
            if n > \(limit) then set n to \(limit)
            set out to ""
            if n > 0 then
                repeat with i from 1 to n
                    set m to message i of inbox
                    set out to out & (subject of m) & "|" & (sender of m) & "|" & ((date received of m) as string) & linefeed
                end repeat
            end if
            return out
        end tell
        """
        launchBundleHeadless("com.apple.mail")
        let output = try runAppleScript(script)
        hideProcess("Mail")
        let messages = output
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { line -> [String: Any] in
                let parts = line.split(separator: "|", maxSplits: 2, omittingEmptySubsequences: false)
                return [
                    "subject": parts.count > 0 ? String(parts[0]) : "",
                    "sender": parts.count > 1 ? String(parts[1]) : "",
                    "received": parts.count > 2 ? String(parts[2]) : "",
                ]
            }
        success(["messages": messages])
    } catch {
        fail(.failed, "failed", "mail.list failed: \(error)")
    }

case "mail.send":
    guard let to = argumentValue("--to"), !to.isEmpty else {
        fail(.badArguments, "bad_arguments", "mail.send requires --to")
    }
    guard let subject = argumentValue("--subject") else {
        fail(.badArguments, "bad_arguments", "mail.send requires --subject")
    }
    let body = argumentValue("--body") ?? ""
    do {
        let script = """
        tell application "Mail" to launch
        tell application "Mail"
            set newMessage to make new outgoing message with properties {subject:"\(appleScriptEscape(subject))", content:"\(appleScriptEscape(body))", visible:false}
            tell newMessage
                make new to recipient at end of to recipients with properties {address:"\(appleScriptEscape(to))"}
                send
            end tell
        end tell
        """
        if arguments.contains("--dry-run") {
            guard automationGranted(for: "com.apple.mail") == true else {
                fail(
                    .permissionDenied,
                    "permission_denied",
                    "Automation for Mail is not granted; a real send would fail. Dry-run aborted."
                )
            }
            _ = try compileAppleScript(script)
            success(["to": to, "subject": subject, "dry_run": true, "compiled": true, "headless": true])
        }
        launchBundleHeadless("com.apple.mail")
        _ = try runAppleScript(script)
        hideProcess("Mail")
        success(["to": to, "subject": subject, "sent": true, "headless": true, "focus_stolen": false])
    } catch {
        fail(.failed, "failed", "mail.send failed: \(error)")
    }

// MARK: - Calls

case "call.place":
    guard let destination = argumentValue("--destination"), !destination.isEmpty else {
        fail(.badArguments, "bad_arguments", "call.place requires --destination")
    }
    let kind = argumentValue("--kind") ?? "tel"
    let urlString: String
    switch kind {
    case "tel": urlString = "tel://\(destination)"
    case "facetime": urlString = "facetime://\(destination)"
    default:
        fail(.badArguments, "bad_arguments", "call.place --kind must be tel or facetime")
    }
    guard let url = URL(string: urlString) else {
        fail(.badArguments, "bad_arguments", "call.place could not build URL from destination")
    }
    // FaceTime/Phone still present their own system call UI — that is macOS,
    // not an EV window. We never activate or fall back to a focus-stealing open.
    if openURLHeadless(url) {
        success([
            "destination": destination,
            "kind": kind,
            "opened": true,
            "headless": true,
            "focus_stolen": false,
            "system_call_ui": true,
        ])
    } else {
        fail(
            .notAvailable,
            "not_available",
            "call.place could not open \(kind) without stealing focus"
        )
    }

case "call.check":
    guard let destination = argumentValue("--destination"), !destination.isEmpty else {
        fail(.badArguments, "bad_arguments", "call.check requires --destination")
    }
    let kind = argumentValue("--kind") ?? "tel"
    let urlString: String
    switch kind {
    case "tel": urlString = "tel://\(destination)"
    case "facetime": urlString = "facetime://\(destination)"
    default:
        fail(.badArguments, "bad_arguments", "call.check --kind must be tel or facetime")
    }
    guard let url = URL(string: urlString) else {
        fail(.badArguments, "bad_arguments", "call.check could not build URL from destination")
    }
    if let handlerURL = NSWorkspace.shared.urlForApplication(toOpen: url) {
        success([
            "destination": destination,
            "kind": kind,
            "available": true,
            "handler": handlerURL.lastPathComponent,
        ])
    } else {
        fail(.notAvailable, "not_available", "no handler found for \(urlString)")
    }

// MARK: - Apps

case "apps.list":
    let query = (argumentValue("--query") ?? "").lowercased()
    let runningOnly = (argumentValue("--running") ?? "false").lowercased() == "true"
    var apps: [[String: Any]] = []
    var seen = Set<String>()
    for app in NSWorkspace.shared.runningApplications {
        guard let bundle = app.bundleIdentifier, app.activationPolicy != .prohibited else { continue }
        if seen.contains(bundle) { continue }
        let name = app.localizedName ?? bundle
        if !query.isEmpty {
            if !name.lowercased().contains(query) && !bundle.lowercased().contains(query) { continue }
        }
        seen.insert(bundle)
        apps.append([
            "name": name,
            "bundle_id": bundle,
            "running": true,
            "frontmost": app.isActive,
            "path": app.bundleURL?.path ?? "",
        ])
    }
    if !runningOnly {
        for directory in ["/Applications", "/System/Applications"] {
            guard let contents = try? FileManager.default.contentsOfDirectory(
                at: URL(fileURLWithPath: directory),
                includingPropertiesForKeys: nil
            ) else { continue }
            for url in contents where url.pathExtension == "app" {
                let bundle = Bundle(url: url)
                let identifier = bundle?.bundleIdentifier ?? url.deletingPathExtension().lastPathComponent
                if seen.contains(identifier) { continue }
                let name = (bundle?.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String)
                    ?? (bundle?.object(forInfoDictionaryKey: "CFBundleName") as? String)
                    ?? url.deletingPathExtension().lastPathComponent
                if !query.isEmpty {
                    if !name.lowercased().contains(query) && !identifier.lowercased().contains(query) {
                        continue
                    }
                }
                seen.insert(identifier)
                apps.append([
                    "name": name,
                    "bundle_id": identifier,
                    "running": false,
                    "frontmost": false,
                    "path": url.path,
                ])
            }
        }
    }
    success(["apps": Array(apps.prefix(40)), "count": min(apps.count, 40)])

case "apps.frontmost":
    let app = NSWorkspace.shared.frontmostApplication
    success([
        "name": app?.localizedName ?? "unknown",
        "bundle_identifier": app?.bundleIdentifier ?? "",
    ])

case "apps.activate":
    guard let bundleID = argumentValue("--bundle-id") else {
        fail(.badArguments, "bad_arguments", "apps.activate requires --bundle-id")
    }
    if let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first {
        app.activate(options: [.activateAllWindows])
        success(["bundle_identifier": bundleID, "activated": true, "launched": false])
    } else if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
        let launched = NSWorkspace.shared.open(url)
        success(["bundle_identifier": bundleID, "activated": launched, "launched": launched])
    } else {
        fail(.notAvailable, "not_available", "no app found for bundle id \(bundleID)")
    }

case "apps.quit":
    guard let bundleID = argumentValue("--bundle-id") else {
        fail(.badArguments, "bad_arguments", "apps.quit requires --bundle-id")
    }
    let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
    guard let app = running.first else {
        success([
            "bundle_identifier": bundleID,
            "quit": true,
            "was_running": false,
            "already_closed": true,
        ])
    }
    let quit = app.terminate()
    success([
        "bundle_identifier": bundleID,
        "quit": quit,
        "was_running": true,
        "already_closed": false,
    ])

case "open.url":
    guard let urlString = argumentValue("--url"), !urlString.isEmpty else {
        fail(.badArguments, "bad_arguments", "open.url requires --url")
    }
    let allowedURLSchemes: Set<String> = [
        "http", "https", "mailto", "maps", "message", "sms", "tel",
        "facetime", "spotify", "notes", "music", "itms", "itmss",
    ]
    guard let url = URL(string: urlString),
          let scheme = url.scheme?.lowercased(),
          allowedURLSchemes.contains(scheme)
    else {
        fail(.badArguments, "bad_arguments", "open.url only accepts web and allowlisted app URLs")
    }
    if NSWorkspace.shared.open(url) {
        success(["url": urlString, "opened": true])
    } else {
        fail(.notAvailable, "not_available", "could not open \(urlString)")
    }

default:
    fail(.badArguments, "bad_arguments", "unknown command \(command)")
}

// MARK: - Implementations

func requireContacts() throws -> CNContactStore {
    let status = CNContactStore.authorizationStatus(for: .contacts)
    guard status == .authorized else {
        fail(
            .permissionDenied,
            "permission_denied",
            "Contacts permission not granted. Grant EV in System Settings → Privacy & Security → Contacts."
        )
    }
    return CNContactStore()
}

func fetchContacts(query: String?) throws -> [[String: Any]] {
    let store = try requireContacts()
    let keys: [CNKeyDescriptor] = [
        CNContactIdentifierKey as CNKeyDescriptor,
        CNContactGivenNameKey as CNKeyDescriptor,
        CNContactFamilyNameKey as CNKeyDescriptor,
        CNContactPhoneNumbersKey as CNKeyDescriptor,
        CNContactEmailAddressesKey as CNKeyDescriptor,
    ]
    let request = CNContactFetchRequest(keysToFetch: keys)
    var contacts: [[String: Any]] = []
    try store.enumerateContacts(with: request) { contact, _ in
        let phoneNumbers = contact.phoneNumbers.map { $0.value.stringValue }
        let emailAddresses = contact.emailAddresses.map { $0.value as String }
        let fullName = "\(contact.givenName) \(contact.familyName)"
            .trimmingCharacters(in: .whitespaces)
        if let query, !query.isEmpty {
            let haystack = "\(fullName) \(phoneNumbers.joined(separator: " ")) \(emailAddresses.joined(separator: " "))"
                .lowercased()
            guard haystack.contains(query.lowercased()) else { return }
        }
        contacts.append([
            "id": contact.identifier,
            "given_name": contact.givenName,
            "family_name": contact.familyName,
            "full_name": fullName,
            "phone_numbers": phoneNumbers,
            "email_addresses": emailAddresses,
        ])
    }
    return contacts
}

func createContact(name: String, phone: String?, email: String?, company: String?) throws -> [String: Any] {
    let store = try requireContacts()
    let contact = CNMutableContact()
    let parts = name.split(separator: " ", maxSplits: 1)
    contact.givenName = String(parts.first ?? "")
    if parts.count > 1 {
        contact.familyName = String(parts[1])
    }
    if let phone, !phone.isEmpty {
        contact.phoneNumbers = [CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: phone))]
    }
    if let email, !email.isEmpty {
        contact.emailAddresses = [CNLabeledValue(label: CNLabelHome, value: email as NSString)]
    }
    if let company, !company.isEmpty {
        contact.organizationName = company
    }
    let saveRequest = CNSaveRequest()
    saveRequest.add(contact, toContainerWithIdentifier: nil)
    try store.execute(saveRequest)
    return [
        "id": contact.identifier,
        "full_name": name,
        "phone": phone ?? "",
        "email": email ?? "",
        "company": company ?? "",
        "created": true,
    ]
}

func updateContact(identifier: String?, query: String?, name: String?, phone: String?, email: String?, company: String?) throws -> [String: Any] {
    let store = try requireContacts()
    let keys: [CNKeyDescriptor] = [
        CNContactIdentifierKey as CNKeyDescriptor,
        CNContactGivenNameKey as CNKeyDescriptor,
        CNContactFamilyNameKey as CNKeyDescriptor,
        CNContactPhoneNumbersKey as CNKeyDescriptor,
        CNContactEmailAddressesKey as CNKeyDescriptor,
        CNContactOrganizationNameKey as CNKeyDescriptor,
    ]
    var targetContact: CNContact?
    if let identifier, !identifier.isEmpty {
        targetContact = try? store.unifiedContact(withIdentifier: identifier, keysToFetch: keys)
    }
    if targetContact == nil, let query, !query.isEmpty {
        let request = CNContactFetchRequest(keysToFetch: keys)
        try store.enumerateContacts(with: request) { contact, stop in
            let fullName = "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces).lowercased()
            if fullName.contains(query.lowercased()) {
                targetContact = contact
                stop.pointee = true
            }
        }
    }
    guard let found = targetContact else {
        throw LifeError.failed("contact not found")
    }
    guard let mutable = found.mutableCopy() as? CNMutableContact else {
        throw LifeError.failed("could not create mutable contact copy")
    }
    if let name, !name.isEmpty {
        let parts = name.split(separator: " ", maxSplits: 1)
        mutable.givenName = String(parts.first ?? "")
        mutable.familyName = parts.count > 1 ? String(parts[1]) : ""
    }
    if let phone, !phone.isEmpty {
        mutable.phoneNumbers = [CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: phone))]
    }
    if let email, !email.isEmpty {
        mutable.emailAddresses = [CNLabeledValue(label: CNLabelHome, value: email as NSString)]
    }
    if let company, !company.isEmpty {
        mutable.organizationName = company
    }
    let saveRequest = CNSaveRequest()
    saveRequest.update(mutable)
    try store.execute(saveRequest)
    let updatedFullName = "\(mutable.givenName) \(mutable.familyName)".trimmingCharacters(in: .whitespaces)
    return [
        "id": mutable.identifier,
        "full_name": updatedFullName,
        "phone": phone ?? "",
        "email": email ?? "",
        "company": company ?? "",
        "updated": true,
    ]
}

func listMessages(limit: Int) throws -> [[String: Any]] {
    let dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
    guard FileManager.default.isReadableFile(atPath: dbPath) else {
        fail(
            .permissionDenied,
            "permission_denied",
            "Messages database requires Full Disk Access. Grant EV in System Settings → Privacy & Security → Full Disk Access."
        )
    }
    let sqlite = URL(fileURLWithPath: "/usr/bin/sqlite3")
    guard FileManager.default.isExecutableFile(atPath: sqlite.path) else {
        fail(.notAvailable, "not_available", "sqlite3 is not available on this system")
    }
    let task = Process()
    task.executableURL = sqlite
    task.arguments = [
        "-separator", "\t",
        dbPath,
        "SELECT message.ROWID, message.date, message.text, handle.id FROM message LEFT JOIN handle ON message.handle_id = handle.ROWID ORDER BY message.date DESC LIMIT \(max(1, limit));",
    ]
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = Pipe()
    try task.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    task.waitUntilExit()
    guard task.terminationStatus == 0,
          let output = String(data: data, encoding: .utf8) else {
        throw LifeError.failed("sqlite3 exit \(task.terminationStatus)")
    }
    return output
        .split(separator: "\n", omittingEmptySubsequences: true)
        .compactMap { line -> [String: Any]? in
            let parts = line.split(separator: "\t", maxSplits: 3, omittingEmptySubsequences: false)
            guard parts.count >= 4 else { return nil }
            let rawReference = Double(parts[1]) ?? 0
            // Modern chat.db stores nanoseconds since 2001; older stores seconds.
            let referenceSeconds = abs(rawReference) > 1_000_000_000_000
                ? rawReference / 1_000_000_000
                : rawReference
            let date = Date(timeIntervalSinceReferenceDate: referenceSeconds)
            return [
                "id": String(parts[0]),
                "date": ISO8601DateFormatter().string(from: date),
                "text": String(parts[2]),
                "handle": String(parts[3]),
            ]
        }
}
