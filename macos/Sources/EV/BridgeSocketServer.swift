import AppKit
import ApplicationServices
import AVFoundation
import Contacts
import CoreGraphics
import CryptoKit
import EventKit
import Foundation
import OSAKit
import Security

/// Headless privileged bridge: a unix-socket JSON server that runs inside
/// Evie.app's TCC identity so headless callers (the Python backend, `ev
/// setup`) can perform permission-gated operations without a UI WebSocket.
///
/// Protocol: one JSON object per line per connection.
/// Request:  {"id": "...", "token": "...", "op": "...", "args": {...}}
/// Response: {"id": "...", "ok": true, "result": {...}}
///           {"id": "...", "ok": false, "error": "...", "fix": "...?"}
///
/// Auth is two layers: the peer's uid must equal ours (LOCAL_PEERCRED) AND
/// the token must match ~/.ev/bridge-token (0600, owner-only). Either check
/// failing yields ok:false — never a connection drop that hides the cause.
final class BridgeSocketServer: @unchecked Sendable {
    static let socketPath: String = ("~/.ev/bridge.sock" as NSString).expandingTildeInPath
    static let tokenPath: String = ("~/.ev/bridge-token" as NSString).expandingTildeInPath
    static let shotsDir: String = ("~/.ev/bridge-shots" as NSString).expandingTildeInPath

    private var listenFD: Int32 = -1
    private var running = false
    private let queue = DispatchQueue(label: "com.ev.suit.bridge", qos: .utility)
    private var token: String = ""

    var isRunning: Bool { running && listenFD >= 0 }

    func start() {
        guard !running else { return }
        // A client that times out and goes away must never SIGPIPE the app:
        // ignore the signal process-wide and set per-socket NOSIGPIPE below.
        signal(SIGPIPE, SIG_IGN)
        running = true
        queue.async { [weak self] in self?.serve() }
    }

    func stop() {
        running = false
        if listenFD >= 0 {
            Darwin.shutdown(listenFD, Int32(SHUT_RDWR))
            Darwin.close(listenFD)
            listenFD = -1
        }
    }

    // MARK: - Serve

    private func serve() {
        token = Self.ensureToken()
        Self.ensureDir(Self.shotsDir)
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return }
        listenFD = fd
        unlink(Self.socketPath)
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            Self.socketPath.withCString { src in
                _ = strcpy(UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self), src)
            }
        }
        let bound = withUnsafePointer(to: addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sptr in
                Darwin.bind(fd, sptr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0 else { Darwin.close(fd); listenFD = -1; return }
        chmod(Self.socketPath, 0o600)
        guard Darwin.listen(fd, 8) == 0 else { Darwin.close(fd); listenFD = -1; return }
        while running {
            var peer = sockaddr()
            var peerLen = socklen_t(MemoryLayout<sockaddr>.size)
            let conn = withUnsafeMutablePointer(to: &peer) { ptr in
                Darwin.accept(fd, ptr, &peerLen)
            }
            guard conn >= 0 else {
                if running { Thread.sleep(forTimeInterval: 0.1) }
                continue
            }
            // Per-connection concurrency: a slow op (AX dump, screenshot)
            // must never head-of-line-block pings and verifiers.
            DispatchQueue.global(qos: .utility).async { [weak self] in
                self?.handle(conn)
                Darwin.close(conn)
            }
        }
        Darwin.close(fd)
        listenFD = -1
        unlink(Self.socketPath)
    }

    private func handle(_ conn: Int32) {
        var tv = timeval(tv_sec: 30, tv_usec: 0)
        setsockopt(conn, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(conn, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var nosigpipe: Int32 = 1
        setsockopt(conn, SOL_SOCKET, SO_NOSIGPIPE, &nosigpipe, socklen_t(MemoryLayout<Int32>.size))
        guard let line = readLine(conn), let data = line.data(using: .utf8),
              let req = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            writeLine(conn, #"{"id":null,"ok":false,"error":"bad_request"}"#)
            return
        }
        let idValue = req["id"] ?? NSNull()
        func reply(ok: Bool, result: Any? = nil, error: String? = nil, fix: String? = nil) {
            var out: [String: Any] = ["id": idValue, "ok": ok]
            if let result { out["result"] = result }
            if let error { out["error"] = error }
            if let fix { out["fix"] = fix }
            let bytes = (try? JSONSerialization.data(withJSONObject: out)) ?? Data()
            writeLine(conn, String(data: bytes, encoding: .utf8) ?? #"{"ok":false}"#)
        }
        guard peerUID(conn) == geteuid() else {
            reply(ok: false, error: "peer_not_owner")
            return
        }
        guard let presented = req["token"] as? String, !presented.isEmpty,
              presented == token
        else {
            reply(ok: false, error: "bad_token")
            return
        }
        let op = (req["op"] as? String ?? "").lowercased()
        let args = (req["args"] as? [String: Any]) ?? [:]
        switch op {
        case "ping":
            reply(ok: true, result: ["op": "ping", "app": "Evie"])
        case "version":
            reply(ok: true, result: [
                "app": "Evie", "bundle": Bundle.main.bundleIdentifier ?? "",
                "git": BuildInfo.gitSHA, "built": BuildInfo.buildTimestamp,
            ])
        case "tcc_status":
            reply(ok: true, result: ["permissions": Self.tccStatus()])
        case "screenshot":
            let out = self.screenshot(args)
            reply(ok: out.ok, result: out.result, error: out.error, fix: out.fix)
        case "jxa":
            let out = self.runJXA(args)
            reply(ok: out.ok, result: out.result, error: out.error, fix: out.fix)
        case "open_url":
            let out = self.openURL(args)
            reply(ok: out.ok, result: out.result, error: out.error, fix: out.fix)
        case "shortcuts_run":
            let out = self.runShortcut(args)
            reply(ok: out.ok, result: out.result, error: out.error, fix: out.fix)
        case "pasteboard_get":
            reply(ok: true, result: ["text": NSPasteboard.general.string(forType: .string) ?? ""])
        case "pasteboard_set":
            let out = self.pasteboardSet(args)
            reply(ok: out.ok, result: out.result, error: out.error, fix: out.fix)
        case "probe_read":
            let out = self.probeRead(args)
            reply(ok: out.ok, result: out.result, error: out.error, fix: out.fix)
        default:
            reply(ok: false, error: "unknown_op:\(op)")
        }
    }

    typealias OpOut = (ok: Bool, result: [String: Any]?, error: String?, fix: String?)

    // MARK: - Ops

    /// Prompt-free TCC truth (except screen recording, which reports the
    /// preflight state — the real verifier is a screenshot attempt).
    static func tccStatus() -> [[String: String]] {
        func row(_ name: String, _ status: String, _ detail: String) -> [String: String] {
            ["permission": name, "status": status, "detail": detail]
        }
        var rows: [[String: String]] = []
        rows.append(row("accessibility", AXIsProcessTrusted() ? "granted" : "denied",
                        "AXIsProcessTrusted (prompt-free)"))
        rows.append(row("screen_recording", CGPreflightScreenCaptureAccess() ? "granted" : "denied",
                        "CGPreflightScreenCaptureAccess"))
        let mic = AVCaptureDevice.authorizationStatus(for: .audio)
        rows.append(row("microphone", mic == .authorized ? "granted" : (mic == .notDetermined ? "unknown" : "denied"),
                        "AVCaptureDevice.authorizationStatus(audio)=\(mic.rawValue)"))
        rows.append(row("input_monitoring", CGPreflightListenEventAccess() ? "granted" : "denied",
                        "CGPreflightListenEventAccess (prompt-free)"))
        rows.append(row("contacts", cnContactsStatus(), "CNContactStore.authorizationStatus"))
        rows.append(row("calendars", ekStatus(.event), "EKEventStore.authorizationStatus"))
        rows.append(row("reminders", ekStatus(.reminder), "EKEventStore.authorizationStatus"))
        rows.append(row("full_disk_access", probeFDARow(), "read probe of ~/Library/Messages/chat.db"))
        return rows
    }

    private static func cnContactsStatus() -> String {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return "granted"
        case .notDetermined: return "unknown"
        case .denied, .restricted: return "denied"
        @unknown default: return "unknown"
        }
    }

    private static func ekStatus(_ type: EKEntityType) -> String {
        switch EKEventStore.authorizationStatus(for: type) {
        case .authorized, .fullAccess: return "granted"
        case .notDetermined: return "unknown"
        case .denied, .restricted: return "denied"
        case .writeOnly: return "write_only"
        @unknown default: return "unknown"
        }
    }

    private static func probeFDARow() -> String {
        let candidates = [
            ("~/Library/Messages/chat.db" as NSString).expandingTildeInPath,
            ("~/Library/Mail/V10/MailData/Envelope Index" as NSString).expandingTildeInPath,
        ]
        for path in candidates {
            if FileManager.default.isReadableFile(atPath: path) { return "granted" }
        }
        return FileManager.default.fileExists(atPath: candidates[0]) ? "denied" : "unknown"
    }

    private func screenshot(_ args: [String: Any]) -> OpOut {
        _ = args
        guard let image = CGWindowListCreateImage(
            CGRectInfinite, .optionOnScreenOnly, kCGNullWindowID, .bestResolution
        ) else {
            return (false, nil, "capture_failed",
                    "System Settings > Privacy & Security > Screen Recording > Evie.")
        }
        let rep = NSBitmapImageRep(cgImage: image)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            return (false, nil, "encode_failed", nil)
        }
        let name = "shot-\(Int(Date().timeIntervalSince1970 * 1000)).png"
        let path = (Self.shotsDir as NSString).appendingPathComponent(name)
        do {
            try png.write(to: URL(fileURLWithPath: path))
        } catch {
            return (false, nil, "write_failed:\(error.localizedDescription)", nil)
        }
        let sha = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined()
        return (true, ["path": path, "sha256": sha,
                       "width": image.width, "height": image.height, "bytes": png.count], nil, nil)
    }

    private func runJXA(_ args: [String: Any]) -> OpOut {
        guard let script = args["script"] as? String, !script.isEmpty else {
            return (false, nil, "empty_script", nil)
        }
        let requested = ((args["language"] as? String) ?? "auto").lowercased()
        let useAppleScript: Bool
        if requested == "applescript" { useAppleScript = true }
        else if requested == "javascript" { useAppleScript = false }
        else {
            useAppleScript = script.range(
                of: "\\btell\\b", options: [.regularExpression, .caseInsensitive]
            ) != nil
        }
        var errorInfo: NSDictionary?
        guard let language = OSALanguage(forName: useAppleScript ? "AppleScript" : "JavaScript") else {
            return (false, nil, "jxa_unavailable", nil)
        }
        let osa = OSAScript(source: script, language: language)
        let result = osa.executeAndReturnError(&errorInfo)
        if let info = errorInfo {
            let code = (info[OSAScriptErrorNumber] as? NSNumber)?.intValue ?? 0
            let message = (info[OSAScriptErrorMessage] as? String) ?? String(describing: info)
            if code == -1743 || code == -1744 {
                return (false, ["code": code], "automation_denied",
                        "System Settings > Privacy & Security > Automation > Evie.")
            }
            return (false, ["code": code], "jxa_failed:\(message)", nil)
        }
        return (true, ["out": result?.stringValue ?? ""], nil, nil)
    }

    private func openURL(_ args: [String: Any]) -> OpOut {
        guard let raw = args["url"] as? String, let url = URL(string: raw),
              ["http", "https", "evie", "shortcuts"].contains(url.scheme?.lowercased() ?? "")
        else {
            return (false, nil, "bad_url", nil)
        }
        NSWorkspace.shared.open(url)
        return (true, ["opened": raw], nil, nil)
    }

    private func runShortcut(_ args: [String: Any]) -> OpOut {
        guard let name = args["name"] as? String, !name.isEmpty else {
            return (false, nil, "empty_name", nil)
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/shortcuts")
        var argv = ["run", name]
        if let input = args["input"] as? String, !input.isEmpty {
            argv += ["--input-path", "-"]
            task.standardInput = Pipe()
        }
        task.arguments = argv
        let outPipe = Pipe()
        let errPipe = Pipe()
        task.standardOutput = outPipe
        task.standardError = errPipe
        do {
            try task.run()
            if let input = args["input"] as? String, !input.isEmpty,
               let stdin = task.standardInput as? Pipe {
                stdin.fileHandleForWriting.write(Data(input.utf8))
                try? stdin.fileHandleForWriting.close()
            }
            task.waitUntilExit()
        } catch {
            return (false, nil, "spawn_failed:\(error.localizedDescription)", nil)
        }
        let out = String(data: outPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let err = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard task.terminationStatus == 0 else {
            return (false, ["exit": task.terminationStatus], "shortcut_failed:\(err)", nil)
        }
        return (true, ["out": out], nil, nil)
    }

    private func pasteboardSet(_ args: [String: Any]) -> OpOut {
        NSPasteboard.general.clearContents()
        let text = args["text"] as? String ?? ""
        NSPasteboard.general.setString(text, forType: .string)
        let back = NSPasteboard.general.string(forType: .string) ?? ""
        guard back == text else { return (false, nil, "readback_mismatch", nil) }
        return (true, ["bytes": back.count], nil, nil)
    }

    private func probeRead(_ args: [String: Any]) -> OpOut {
        guard let raw = args["path"] as? String, !raw.isEmpty else {
            return (false, nil, "empty_path", nil)
        }
        let expanded = (raw as NSString).expandingTildeInPath
        let readable = FileManager.default.isReadableFile(atPath: expanded)
        return (true, ["path": expanded, "readable": readable], nil, nil)
    }

    // MARK: - Socket plumbing

    private func readLine(_ conn: Int32) -> String? {
        var bytes: [UInt8] = []
        bytes.reserveCapacity(4096)
        var byte: UInt8 = 0
        while bytes.count < 4_000_000 {
            let got = Darwin.recv(conn, &byte, 1, 0)
            if got <= 0 { break }
            if byte == 10 { break }
            bytes.append(byte)
        }
        guard !bytes.isEmpty else { return nil }
        return String(bytes: bytes, encoding: .utf8)
    }

    private func writeLine(_ conn: Int32, _ text: String) {
        let data = Data((text + "\n").utf8)
        data.withUnsafeBytes { ptr in
            var sent = 0
            while sent < data.count {
                let got = Darwin.send(conn, ptr.baseAddress!.advanced(by: sent), data.count - sent, 0)
                if got <= 0 { break }
                sent += got
            }
        }
    }

    private func peerUID(_ conn: Int32) -> uid_t {
        var cred = xucred()
        var len = socklen_t(MemoryLayout<xucred>.size)
        let got = getsockopt(conn, 0, LOCAL_PEERCRED, &cred, &len)
        guard got == 0 else { return uid_t(UInt32.max) }
        return cred.cr_uid
    }

    private static func ensureToken() -> String {
        let fm = FileManager.default
        if let saved = try? String(contentsOfFile: tokenPath, encoding: .utf8),
           saved.trimmingCharacters(in: .whitespacesAndNewlines).count >= 32 {
            return saved.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        let token = bytes.map { String(format: "%02x", $0) }.joined()
        try? token.write(toFile: tokenPath, atomically: true, encoding: .utf8)
        chmod(tokenPath, 0o600)
        return token
    }

    private static func ensureDir(_ path: String) {
        try? FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true)
    }
}
