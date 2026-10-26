// evhands — Accessibility + pointer helper for HANDS (docs/contracts/SKILLS_API.md).
//
// Native AX calls take microseconds each; the same walk through System Events
// JXA takes seconds. Every command prints ONE JSON object on stdout:
//   {"ok":true,...}  or  {"ok":false,"error":{"code":"...","message":"...","fix":"..."}}
//
// Commands
//   trusted                              Accessibility trust of THIS process
//   enable <app>                         turn on web/AX tree for Chromium-style apps
//   windows <app>                        window titles + frames
//   panes <app> [--title T]              AXTextArea frames/focus/text (terminal panes)
//   tree <app> [--depth N] [--limit N] [--title T]
//   find <app> [--role R] [--name REGEX] [--title T] [--max N] [--depth N]
//   act <app> --path 0.3.1 --do press|set-value|focus|raise|show-menu [--value V] [--title T]
//   click <x> <y> [--clicks N] [--button left|right]    trusted pointer click
//   key <keycode> [--mods cmd,shift,option,control]     trusted key press
//   cgwindows [--owner NAME]             on-screen windows (number, owner, title, bounds)
//   setframe <app> [--title T] --x X --y Y --w W --h H    move/resize a window
//   automation <bundle-id>               Apple Events consent for that app, WITHOUT prompting
//   screen                               Screen Recording consent, WITHOUT prompting
//
// <app> is a localized name or bundle id. Paths are child indexes below the window.

import ApplicationServices
import AppKit
import Foundation

let version = "1"

func out(_ obj: [String: Any]) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit((obj["ok"] as? Bool) == true ? 0 : 2)
}

func fail(_ code: String, _ message: String, fix: String? = nil) -> Never {
    var e: [String: Any] = ["code": code, "message": message]
    if let fix = fix { e["fix"] = fix }
    out(["ok": false, "error": e])
}

func attr(_ el: AXUIElement, _ name: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func str(_ el: AXUIElement, _ name: String) -> String? {
    if let v = attr(el, name) {
        if let s = v as? String { return s }
        if let n = v as? NSNumber { return n.stringValue }
    }
    return nil
}

func point(_ el: AXUIElement) -> [Double]? {
    guard let v = attr(el, kAXPositionAttribute as String), CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
    var p = CGPoint.zero
    AXValueGetValue(v as! AXValue, .cgPoint, &p)
    return [Double(p.x), Double(p.y)]
}

func size(_ el: AXUIElement) -> [Double]? {
    guard let v = attr(el, kAXSizeAttribute as String), CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
    var s = CGSize.zero
    AXValueGetValue(v as! AXValue, .cgSize, &s)
    return [Double(s.width), Double(s.height)]
}

func kids(_ el: AXUIElement) -> [AXUIElement] {
    (attr(el, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
}

func boolAttr(_ el: AXUIElement, _ name: String) -> Bool? {
    (attr(el, name) as? NSNumber)?.boolValue
}

func actions(_ el: AXUIElement) -> [String] {
    var names: CFArray?
    if AXUIElementCopyActionNames(el, &names) == .success, let a = names as? [String] { return a }
    return []
}

func findApp(_ key: String) -> NSRunningApplication? {
    let k = key.lowercased()
    return NSWorkspace.shared.runningApplications.first {
        $0.activationPolicy != .prohibited &&
            (($0.localizedName ?? "").lowercased() == k || ($0.bundleIdentifier ?? "").lowercased() == k)
    }
}

func appElement(_ key: String) -> AXUIElement {
    guard AXIsProcessTrusted() else {
        fail("permission_missing", "Accessibility is not granted to this process",
             fix: "System Settings > Privacy & Security > Accessibility: allow the app that runs evhands.")
    }
    guard let app = findApp(key) else { fail("not_running", "\(key) is not running") }
    return AXUIElementCreateApplication(app.processIdentifier)
}

func windowsOf(_ app: AXUIElement) -> [AXUIElement] {
    (attr(app, kAXWindowsAttribute as String) as? [AXUIElement]) ?? []
}

func node(_ el: AXUIElement, valueLimit: Int = 300) -> [String: Any] {
    var d: [String: Any] = ["role": str(el, kAXRoleAttribute as String) ?? ""]
    if let s = str(el, kAXSubroleAttribute as String) { d["subrole"] = s }
    if let s = str(el, kAXTitleAttribute as String), !s.isEmpty { d["title"] = s }
    if let s = str(el, kAXDescriptionAttribute as String), !s.isEmpty { d["desc"] = s }
    if let s = str(el, kAXIdentifierAttribute as String), !s.isEmpty { d["id"] = s }
    if let s = str(el, kAXHelpAttribute as String), !s.isEmpty { d["help"] = s }
    if let v = attr(el, kAXValueAttribute as String) {
        if let s = v as? String { d["value"] = String(s.prefix(valueLimit)) }
        else if let n = v as? NSNumber { d["value"] = n }
    }
    if let e = boolAttr(el, kAXEnabledAttribute as String) { d["enabled"] = e }
    if let f = boolAttr(el, kAXFocusedAttribute as String) { d["focused"] = f }
    if let p = point(el) { d["pos"] = p }
    if let s = size(el) { d["size"] = s }
    let a = actions(el)
    if !a.isEmpty { d["actions"] = a }
    return d
}

func labels(_ el: AXUIElement) -> [String] {
    [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute, kAXHelpAttribute, kAXIdentifierAttribute]
        .compactMap { str(el, $0 as String) }.filter { !$0.isEmpty }
}

func args() -> [String] { Array(CommandLine.arguments.dropFirst()) }

func opt(_ name: String) -> String? {
    let a = args()
    if let i = a.firstIndex(of: name), i + 1 < a.count { return a[i + 1] }
    return nil
}

func windowsMatching(_ app: AXUIElement) -> [(Int, AXUIElement)] {
    let want = opt("--title")
    return windowsOf(app).enumerated().filter { (_, w) in
        want == nil || (str(w, kAXTitleAttribute as String) ?? "") == want!
    }.map { ($0.offset, $0.element) }
}

// ---------------------------------------------------------------- commands --

func cmdPanes(_ appKey: String) {
    let app = appElement(appKey)
    var wins: [[String: Any]] = []
    for (i, w) in windowsMatching(app) {
        var panes: [[String: Any]] = []
        func walk(_ el: AXUIElement, _ depth: Int) {
            if depth > 14 { return }
            if (str(el, kAXRoleAttribute as String) ?? "") == "AXTextArea" {
                var d = node(el, valueLimit: 4000)
                d["text"] = d.removeValue(forKey: "value") ?? ""
                panes.append(d)
                return
            }
            for k in kids(el) { walk(k, depth + 1) }
        }
        walk(w, 0)
        wins.append(["index": i, "title": str(w, kAXTitleAttribute as String) ?? "",
                     "pos": point(w) ?? [], "size": size(w) ?? [], "panes": panes])
    }
    out(["ok": true, "windows": wins])
}

func cmdTree(_ appKey: String) {
    let app = appElement(appKey)
    let maxDepth = Int(opt("--depth") ?? "6") ?? 6
    let limit = Int(opt("--limit") ?? "400") ?? 400
    var count = 0
    func walk(_ el: AXUIElement, _ path: [Int], _ depth: Int) -> [String: Any] {
        count += 1
        var d = node(el)
        d["path"] = path
        if depth < maxDepth {
            var children: [[String: Any]] = []
            for (i, k) in kids(el).enumerated() {
                if count >= limit { break }
                children.append(walk(k, path + [i], depth + 1))
            }
            if !children.isEmpty { d["children"] = children }
        }
        return d
    }
    var wins: [[String: Any]] = []
    for (i, w) in windowsMatching(app) { wins.append(walk(w, [i], 0)) }
    out(["ok": true, "windows": wins, "count": count, "truncated": count >= limit])
}

func cmdFind(_ appKey: String) {
    let app = appElement(appKey)
    let role = opt("--role")
    let regex = opt("--name").flatMap { try? NSRegularExpression(pattern: $0, options: [.caseInsensitive]) }
    let maxHits = Int(opt("--max") ?? "20") ?? 20
    let maxDepth = Int(opt("--depth") ?? "16") ?? 16
    var hits: [[String: Any]] = []
    var visited = 0
    func walk(_ el: AXUIElement, _ path: [Int], _ depth: Int) {
        if hits.count >= maxHits || depth > maxDepth || visited > 20000 { return }
        visited += 1
        let r = str(el, kAXRoleAttribute as String) ?? ""
        let roleOK = role == nil || r == role!
        if roleOK {
            // each attribute is matched on its own so anchored patterns like ^skip$ work
            let nameOK = regex == nil || labels(el).contains {
                regex!.firstMatch(in: $0, range: NSRange($0.startIndex..., in: $0)) != nil
            }
            if nameOK && (role != nil || regex != nil) {
                var d = node(el)
                d["path"] = path
                hits.append(d)
            }
        }
        for (i, k) in kids(el).enumerated() { walk(k, path + [i], depth + 1) }
    }
    for (i, w) in windowsMatching(app) { walk(w, [i], 0) }
    out(["ok": true, "matches": hits, "visited": visited])
}

func resolve(_ app: AXUIElement, _ path: [Int]) -> AXUIElement? {
    let wins = windowsOf(app)
    guard let first = path.first, first < wins.count else { return nil }
    var el = wins[first]
    for idx in path.dropFirst() {
        let ks = kids(el)
        guard idx < ks.count else { return nil }
        el = ks[idx]
    }
    return el
}

func cmdAct(_ appKey: String) {
    let app = appElement(appKey)
    guard let p = opt("--path"), let verb = opt("--do") else { fail("invalid_args", "act needs --path and --do") }
    let path = p.split(separator: ".").compactMap { Int($0) }
    guard let el = resolve(app, path) else { fail("stale_ref", "no element at path \(p)") }
    var result: AXError = .success
    switch verb {
    case "press": result = AXUIElementPerformAction(el, kAXPressAction as CFString)
    case "show-menu": result = AXUIElementPerformAction(el, kAXShowMenuAction as CFString)
    case "raise": result = AXUIElementPerformAction(el, kAXRaiseAction as CFString)
    case "focus": result = AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    case "set-value":
        guard let v = opt("--value") else { fail("invalid_args", "set-value needs --value") }
        if (str(el, kAXRoleAttribute as String) ?? "") == "AXSecureTextField" {
            fail("denied", "refusing to set a secure text field")
        }
        result = AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, v as CFTypeRef)
    default: fail("invalid_args", "unknown --do \(verb)")
    }
    if result != .success { fail("internal", "AX action \(verb) failed (\(result.rawValue))") }
    var back = node(el)
    back["path"] = path
    out(["ok": true, "did": verb, "element": back])
}

func cmdClick() {
    let a = args()
    guard a.count >= 3, let x = Double(a[1]), let y = Double(a[2]) else { fail("invalid_args", "click <x> <y>") }
    guard AXIsProcessTrusted() else {
        fail("permission_missing", "Accessibility is not granted to this process",
             fix: "System Settings > Privacy & Security > Accessibility: allow the app that runs evhands.")
    }
    let clicks = Int(opt("--clicks") ?? "1") ?? 1
    let right = (opt("--button") ?? "left") == "right"
    let pt = CGPoint(x: x, y: y)
    let down: CGEventType = right ? .rightMouseDown : .leftMouseDown
    let up: CGEventType = right ? .rightMouseUp : .leftMouseUp
    let btn: CGMouseButton = right ? .right : .left
    for n in 1...max(1, clicks) {
        guard let d = CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: pt, mouseButton: btn),
              let u = CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: pt, mouseButton: btn) else {
            fail("internal", "could not create mouse event")
        }
        d.setIntegerValueField(.mouseEventClickState, value: Int64(n))
        u.setIntegerValueField(.mouseEventClickState, value: Int64(n))
        d.post(tap: .cghidEventTap)
        u.post(tap: .cghidEventTap)
    }
    out(["ok": true, "clicked": [x, y], "clicks": clicks])
}

func cmdKey() {
    let a = args()
    guard a.count >= 2, let code = UInt16(a[1]) else { fail("invalid_args", "key <keycode>") }
    var flags = CGEventFlags()
    for m in (opt("--mods") ?? "").split(separator: ",") {
        switch m {
        case "cmd", "command": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "option", "alt": flags.insert(.maskAlternate)
        case "control", "ctrl": flags.insert(.maskControl)
        default: break
        }
    }
    guard let d = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let u = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        fail("internal", "could not create key event")
    }
    d.flags = flags
    u.flags = flags
    d.post(tap: .cghidEventTap)
    u.post(tap: .cghidEventTap)
    out(["ok": true, "key": code])
}

func cmdSetFrame(_ appKey: String) {
    let app = appElement(appKey)
    guard let x = Double(opt("--x") ?? ""), let y = Double(opt("--y") ?? ""),
          let w = Double(opt("--w") ?? ""), let h = Double(opt("--h") ?? "") else {
        fail("invalid_args", "setframe needs --x --y --w --h")
    }
    guard let (_, win) = windowsMatching(app).first else { fail("not_found", "no such window") }
    var pt = CGPoint(x: x, y: y)
    var sz = CGSize(width: w, height: h)
    guard let pv = AXValueCreate(.cgPoint, &pt), let sv = AXValueCreate(.cgSize, &sz) else {
        fail("internal", "could not build AX values")
    }
    let r1 = AXUIElementSetAttributeValue(win, kAXPositionAttribute as CFString, pv)
    let r2 = AXUIElementSetAttributeValue(win, kAXSizeAttribute as CFString, sv)
    if r1 != .success || r2 != .success { fail("internal", "AX refused the new frame (\(r1.rawValue)/\(r2.rawValue))") }
    out(["ok": true, "pos": point(win) ?? [], "size": size(win) ?? []])
}

func cmdAutomation() {
    let a = args()
    guard a.count >= 2 else { fail("invalid_args", "automation <bundle-id>") }
    guard let running = NSRunningApplication.runningApplications(withBundleIdentifier: a[1]).first else {
        out(["ok": true, "status": "not_running", "bundle": a[1]])
    }
    var addr = AEAddressDesc()
    var pid = running.processIdentifier
    AECreateDesc(DescType(typeKernelProcessID), &pid, MemoryLayout<pid_t>.size, &addr)
    let r = AEDeterminePermissionToAutomateTarget(&addr, AEEventClass(typeWildCard), AEEventID(typeWildCard), false)
    AEDisposeDesc(&addr)
    let status: String
    switch Int(r) {
    case 0: status = "granted"
    case -1744: status = "undetermined"   // would ask the user on first use
    case -1743: status = "denied"
    default: status = "unknown"
    }
    out(["ok": true, "status": status, "code": Int(r), "bundle": a[1]])
}

func cmdScreen() {
    out(["ok": true, "granted": CGPreflightScreenCaptureAccess()])
}

func cmdCGWindows() {
    let want = opt("--owner")?.lowercased()
    let list = (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]]) ?? []
    var rows: [[String: Any]] = []
    for w in list {
        let owner = (w[kCGWindowOwnerName as String] as? String) ?? ""
        if let want = want, owner.lowercased() != want { continue }
        let b = (w[kCGWindowBounds as String] as? [String: Any]) ?? [:]
        rows.append(["number": (w[kCGWindowNumber as String] as? Int) ?? 0, "owner": owner,
                     "title": (w[kCGWindowName as String] as? String) ?? "",
                     "layer": (w[kCGWindowLayer as String] as? Int) ?? 0,
                     "pid": (w[kCGWindowOwnerPID as String] as? Int) ?? 0,
                     "x": (b["X"] as? Double) ?? 0, "y": (b["Y"] as? Double) ?? 0,
                     "w": (b["Width"] as? Double) ?? 0, "h": (b["Height"] as? Double) ?? 0])
    }
    out(["ok": true, "windows": rows])
}

let a = args()
guard let cmd = a.first else { fail("invalid_args", "usage: evhands <command> ...  (version \(version))") }
switch cmd {
case "version": out(["ok": true, "version": version])
case "trusted": out(["ok": true, "trusted": AXIsProcessTrusted()])
case "click": cmdClick()
case "key": cmdKey()
case "cgwindows": cmdCGWindows()
case "automation": cmdAutomation()
case "screen": cmdScreen()
default:
    guard a.count >= 2 else { fail("invalid_args", "\(cmd) needs an app") }
    switch cmd {
    case "enable":
        let app = appElement(a[1])
        let r1 = AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        let r2 = AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
        out(["ok": true, "manual": r1 == .success, "enhanced": r2 == .success])
    case "windows":
        let app = appElement(a[1])
        out(["ok": true, "windows": windowsOf(app).enumerated().map { (i, w) -> [String: Any] in
            ["index": i, "title": str(w, kAXTitleAttribute as String) ?? "",
             "pos": point(w) ?? [], "size": size(w) ?? []]
        }])
    case "panes": cmdPanes(a[1])
    case "tree": cmdTree(a[1])
    case "find": cmdFind(a[1])
    case "act": cmdAct(a[1])
    case "setframe": cmdSetFrame(a[1])
    default: fail("invalid_args", "unknown command \(cmd)")
    }
}
