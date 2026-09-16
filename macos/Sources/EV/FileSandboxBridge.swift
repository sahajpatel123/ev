import Foundation

/// Evie file sandbox — Mac runner.
///
/// Same verbs, same jail, same receipts as `backend/app/ev/file_sandbox.py`.
/// The brain (Muse Spark 1.3) plans; this bridge executes on the Mac.
/// iPhone-originated commands arrive via Home Station
/// (`device_gateway/phone_mac.py` → live websocket → `MacControlService`)
/// and land on this exact path, so Mac and iPhone never diverge.
///
/// Jail mirrors the backend: ~/Desktop, ~/Documents, ~/Downloads (+ the
/// `EV_LAPTOP_FILES_ROOT` override in tests). Deny list mirrors
/// `laptop_files.path_denied` + `MacControlService.fileDenied`.
public final class FileSandboxBridge: @unchecked Sendable {
    public static let shared = FileSandboxBridge()

    public enum Origin: String {
        case mac, iphone, brain, api
    }

    private init() {}

    // MARK: - Jail

    public func allowedRoots() -> [URL] {
        if let override = ProcessInfo.processInfo.environment["EV_LAPTOP_FILES_ROOT"],
           !override.trimmingCharacters(in: .whitespaces).isEmpty
        {
            return [URL(fileURLWithPath: (override as NSString).expandingTildeInPath)]
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return ["Desktop", "Documents", "Downloads"].map { home.appendingPathComponent($0) }
    }

    private func denied(_ url: URL) -> String? {
        let path = url.resolvingSymlinksInPath().path.lowercased()
        if path.contains("/.ssh/") || path.hasSuffix("/.ssh") { return "path_denied" }
        if path.contains("/.ev/secrets") { return "path_denied" }
        if path.contains("/.git/") { return "path_denied" }
        for suffix in [".pem", ".p12", ".pfx", ".key", ".kdbx"] where path.hasSuffix(suffix) {
            return "path_denied"
        }
        return nil
    }

    private func insideJail(_ url: URL) -> Bool {
        let resolved = url.resolvingSymlinksInPath().path
        return allowedRoots().contains {
            let root = $0.resolvingSymlinksInPath().path
            return resolved == root || resolved.hasPrefix(root + "/")
        }
    }

    // MARK: - Discover / index (read-only, no MacControl needed)

    public func discover(requestId: String) -> [String: Any] {
        let roots = allowedRoots()
        let fm = FileManager.default
        let surveyed = roots.map { root -> [String: Any] in
            let children = (try? fm.contentsOfDirectory(atPath: root.path)) ?? []
            return [
                "root": root.path,
                "name": root.lastPathComponent,
                "exists": fm.fileExists(atPath: root.path),
                "sample": Array(children.filter { !$0.hasPrefix(".") }.sorted().prefix(20)),
            ]
        }
        return [
            "ok": true, "op": "discover", "origin": "mac",
            "request_id": requestId, "command": "file_op",
            "roots": roots.map(\.path), "survey": surveyed,
            "spoken": "Jail covers \(roots.count) roots.",
            "verified": true,
        ]
    }

    /// Filename index via Spotlight (`mdfind`), scoped to the jail.
    /// Falls back to a shallow FileManager walk when `mdfind` is unavailable.
    public func index(query: String = "", requestId: String) -> [String: Any] {
        let needle = query.trimmingCharacters(in: .whitespaces)
        var hits: [String] = []
        if !needle.isEmpty {
            hits = spotlightHits(needle: needle)
            if hits.isEmpty { hits = walkHits(needle: needle) }
        }
        return [
            "ok": true, "op": "index", "origin": "mac",
            "request_id": requestId, "command": "file_op",
            "entries": hits.count, "hits": Array(hits.prefix(40)),
            "spoken": hits.isEmpty ? "Index ready." : "Index holds \(hits.count) hits for \(needle).",
            "verified": true,
        ]
    }

    private func spotlightHits(needle: String) -> [String] {
        let roots = allowedRoots()
        var out: [String] = []
        for root in roots {
            let proc = ProcessInfo.processInfo
            _ = proc
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/mdfind")
            task.arguments = ["-onlyin", root.path, "-name", needle]
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = FileHandle.nullDevice
            do {
                try task.run()
                task.waitUntilExit()
                let text = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                for line in text.split(separator: "\n").map(String.init) where !line.isEmpty {
                    let url = URL(fileURLWithPath: line)
                    if insideJail(url), denied(url) == nil { out.append(line) }
                    if out.count >= 80 { return out }
                }
            } catch { continue }
        }
        return out
    }

    private func walkHits(needle: String) -> [String] {
        let lowered = needle.lowercased()
        var out: [String] = []
        let fm = FileManager.default
        for root in allowedRoots() {
            guard let enumerator = fm.enumerator(atPath: root.path) else { continue }
            var depth = 0
            for case let name as String in enumerator {
                depth += 1
                if depth > 4000 { break }
                if name.lowercased().contains(lowered) {
                    let url = root.appendingPathComponent(name)
                    if insideJail(url), denied(url) == nil { out.append(url.path) }
                    if out.count >= 80 { return out }
                }
            }
        }
        return out
    }

    // MARK: - DDL / DML (through the authoritative MacControl boundary)

    /// Run one sandbox verb on this Mac. `origin` stamps the receipt so
    /// iPhone-relayed runs (`origin: "iphone"`) stay distinguishable while
    /// executing the identical code path as Mac-spoken runs.
    @discardableResult
    public func execute(
        op: String,
        arguments: [String: Any],
        origin: String = "mac",
        requestId: String = UUID().uuidString
    ) -> [String: Any] {
        let name = op.lowercased()
        switch name {
        case "discover":
            var payload = discover(requestId: requestId)
            payload["origin"] = origin
            return payload
        case "index", "search":
            var payload = index(query: (arguments["query"] as? String) ?? "", requestId: requestId)
            payload["op"] = "search"
            payload["origin"] = origin
            return payload
        case "read", "list", "write", "edit", "append", "mkdir", "delete", "copy", "move", "rename", "run", "open":
            var args = arguments
            // mkdir has no file_op action yet: create the folder under the jail here.
            if name == "mkdir", let raw = (args["path"] as? String), !raw.isEmpty {
                return mkdir(path: raw, origin: origin, requestId: requestId)
            }
            // Backend edit/append take full bodies; file_op write covers both.
            if name == "edit" || name == "append" { args["action"] = "write" }
            else if name == "list" || name == "read" || name == "write" || name == "delete"
                || name == "copy" || name == "move" || name == "rename" || name == "run" || name == "open"
            { args["action"] = name }
            var receipt = MacControlService.shared.handle(command: "file_op", arguments: args, requestId: requestId)
            receipt["op"] = name
            receipt["origin"] = origin
            return receipt
        case "undo":
            return [
                "ok": false, "op": "undo", "origin": origin,
                "request_id": requestId, "command": "file_op",
                "error": "mac_relay_undo",
                "spoken": "Undo lives with the backend journal — ask Evie to undo that on Home Station.",
            ]
        default:
            return [
                "ok": false, "op": name, "origin": origin,
                "request_id": requestId, "command": "file_op",
                "error": "unknown_op",
                "spoken": "I can discover, index, search, read, write, edit, append, mkdir, delete, copy, move, rename, run, or undo files.",
            ]
        }
    }

    private func mkdir(path: String, origin: String, requestId: String) -> [String: Any] {
        let expanded = (path as NSString).expandingTildeInPath
        let url: URL
        if (path as NSString).isAbsolutePath {
            url = URL(fileURLWithPath: expanded)
        } else {
            url = allowedRoots().first?.appendingPathComponent(expanded) ?? URL(fileURLWithPath: expanded)
        }
        guard insideJail(url) else {
            return ["ok": false, "op": "mkdir", "origin": origin, "request_id": requestId, "command": "file_op", "error": "path_outside_allowed", "spoken": "I won't touch that path."]
        }
        if let denied = denied(url) {
            return ["ok": false, "op": "mkdir", "origin": origin, "request_id": requestId, "command": "file_op", "error": denied, "spoken": "I won't touch that path."]
        }
        do {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            return ["ok": true, "op": "mkdir", "origin": origin, "request_id": requestId, "command": "file_op", "path": url.path, "spoken": "Created folder \(url.lastPathComponent).", "verified": true]
        } catch {
            return ["ok": false, "op": "mkdir", "origin": origin, "request_id": requestId, "command": "file_op", "error": "mkdir_failed", "spoken": error.localizedDescription]
        }
    }
}
