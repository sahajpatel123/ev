import AppKit
import Darwin
import EVClient
import Foundation

/// Headless production-path live bridge: same EV.app websocket + MacControlService
/// as Talk, without requiring the owner's microphone.
///
///   macos/build/EV.app/Contents/MacOS/EV --live-e2e --utterance "..."
enum MacControlLiveE2E {
    private static let marker = "EVIE_LIVE_E2E"

    static func run() -> Int32 {
        setbuf(stdout, nil)
        _ = NSApplication.shared
        var code: Int32 = 2
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            code = await execute()
            semaphore.signal()
        }
        while semaphore.wait(timeout: .now() + 0.05) == .timedOut {
            RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
        }
        return code
    }

    private static func execute() async -> Int32 {
        let args = CommandLine.arguments
        let utterance = stringArg(args, "--utterance")
            ?? "Open Music, find the Chess playlist, and play the first track."
        let followUp = stringArg(args, "--follow-up")
        let expect = stringArg(args, "--expect") ?? "music-playing"
        let timeout = Double(stringArg(args, "--timeout") ?? "90") ?? 90
        emit("start", [
            "utterance": utterance,
            "pid": ProcessInfo.processInfo.processIdentifier,
            "executable": Bundle.main.executableURL?.path ?? "",
            "bundle": Bundle.main.bundleIdentifier ?? "",
        ])
        let identity = MacControlService.shared.permissionSnapshot()
        emit("permissions", identity as [String: Any])
        let config = AppConfig()
        if config.usesPlaceholderKey {
            emit("fail", ["boundary": "api_key", "detail": "placeholder API key"])
            return 2
        }
        let token = e2eBearerToken(config.apiKey)
        let client = EVAPIClient(baseURL: config.baseURL, token: token)
        let registry = UserDefaults.standard.string(forKey: "EV_REGISTRY_DEVICE_ID")
        let deviceId: String
        if let registry, UUID(uuidString: registry) != nil {
            deviceId = registry
        } else {
            deviceId = config.deviceID
        }
        do {
            let opened = try await client.openLiveVoice(deviceId: deviceId)
            emit("session_opened", [
                "session_id": opened.sessionId,
                "device_id": deviceId,
                "base_url": config.baseURL.absoluteString,
            ])
            let connection = LiveVoiceConnection(baseURL: config.baseURL, token: token)
            let stream = try await connection.connect(sessionId: opened.sessionId)
            let deadline = Date().addingTimeInterval(timeout)
            var ready = false
            var toolsReady = false
            var sentText = false
            var sentFollowUp = false
            var advertised: [String] = []
            var acknowledged: [String] = []
            var providerSession = ""
            var model = ""
            var schemaHash = ""
            var replies: [String] = []
            var toolCalls: [[String: Any]] = []
            var lastActivity = Date()
            let lock = NSLock()

            func maybeSend() {
                guard ready, toolsReady, !sentText else { return }
                connection.sendText(utterance)
                sentText = true
                lastActivity = Date()
                emit("utterance_sent", ["text": utterance])
            }

            let watchdog = Task {
                let ns = UInt64(max(timeout, 2) * 1_000_000_000)
                try await Task.sleep(nanoseconds: ns)
                emit("watchdog", ["timeout": timeout])
                connection.close()
            }
            let lowerUtterance = utterance.lowercased()
            let wantsLaptopFiles = fileUtterance(lowerUtterance)
            let needsComputerAction = wantsLaptopFiles
                || lowerUtterance.contains("play")
                || lowerUtterance.contains("find")
                || lowerUtterance.contains("search")
                || lowerUtterance.contains("write")
                || lowerUtterance.contains("create")
                || lowerUtterance.contains("calculate")
                || lowerUtterance.contains("jot")
                || lowerUtterance.contains("google")
                || lowerUtterance.contains("multiply")
                || lowerUtterance.contains("type")
                || lowerUtterance.contains("notes")
                || lowerUtterance.contains("safari")
                || lowerUtterance.contains("chrome")
                || lowerUtterance.contains("calculator")
                || lowerUtterance.contains("downloads")
                || lowerUtterance.contains("first result")
            let idleWatch = Task {
                while !Task.isCancelled {
                    try await Task.sleep(nanoseconds: 500_000_000)
                    lock.lock()
                    let sent = sentText
                    let idle = Date().timeIntervalSince(lastActivity)
                    let hasReply = !replies.isEmpty
                    let hasSemantic = toolCalls.contains { computerCommand($0["command"] as? String) }
                    let hasInspect = hasSemantic
                    let actions = toolCalls.compactMap { $0["action"] as? String }
                    var stillGoing = (toolCalls.last?["must_continue"] as? Bool) == true
                    if lowerUtterance.contains("first result") || lowerUtterance.contains("open the first") {
                        stillGoing = stillGoing || !(actions.contains("navigate") || actions.contains("open_item"))
                    }
                    if wantsLaptopFiles {
                        stillGoing = stillGoing || !fileOpSucceeded(toolCalls, utterance: lowerUtterance)
                    } else if lowerUtterance.contains("notes") || lowerUtterance.contains("write") || lowerUtterance.contains("jot") {
                        let wantsRead = lowerUtterance.contains("read")
                            || lowerUtterance.contains("what's in")
                            || lowerUtterance.contains("what is in")
                        let done = toolCalls.contains {
                            let action = ($0["action"] as? String) ?? ""
                            let verified = ($0["verified"] as? Bool) ?? false
                            if wantsRead {
                                return verified && action == "read"
                            }
                            return verified && ["create", "append", "type", "paste", "replace"].contains(action)
                        }
                        stillGoing = stillGoing || !done
                    }
                    if lowerUtterance.contains("calculator") || lowerUtterance.contains("calculate") || lowerUtterance.contains("multiply") {
                        let display = toolCalls.compactMap { $0["display"] as? String }.first { !$0.isEmpty }
                        stillGoing = stillGoing || display == nil
                    }
                    lock.unlock()
                    let progressed = !needsComputerAction || hasSemantic || hasInspect
                    if followUp == nil && sent && idle > 14 && hasReply && progressed && !stillGoing {
                        connection.close()
                        break
                    }
                    if Date() > deadline {
                        connection.close()
                        break
                    }
                }
            }

            for try await event in stream {
                if Date() > deadline { break }
                lock.lock()
                lastActivity = Date()
                lock.unlock()
                if event.type == "ready" {
                    ready = true
                    connection.sendComputerState(
                        MacControlService.shared.permissionSnapshot(),
                        deviceId: deviceId
                    )
                    let realtime = event.config["realtime"]?.objectValue
                    advertised = stringList(realtime?["tool_names"] ?? realtime?["advertised_tool_names"])
                    acknowledged = stringList(realtime?["upstream_tool_names"] ?? realtime?["acknowledged_tool_names"])
                    model = realtime?["model"]?.stringValue ?? event.config["model"]?.stringValue ?? ""
                    providerSession = realtime?["provider_session_id"]?.stringValue ?? ""
                    schemaHash = realtime?["computer_tool_schema_hash"]?.stringValue
                        ?? realtime?["tool_schema_generation"]?.stringValue
                        ?? ""
                    toolsReady = computerSurfaceReady(acknowledged)
                    emit("ready", [
                        "advertised": advertised,
                        "acknowledged": acknowledged,
                        "model": model,
                        "provider_session_id": providerSession,
                        "schema_hash": schemaHash,
                        "tools_ready": toolsReady,
                    ])
                    maybeSend()
                } else if event.type == "realtime_diagnostics" || event.type == "state" {
                    let realtime = event.realtimeDiagnostics
                        ?? event.config["realtime"]?.objectValue
                    if let realtime {
                        advertised = stringList(realtime["tool_names"] ?? realtime["advertised_tool_names"])
                        acknowledged = stringList(realtime["upstream_tool_names"] ?? realtime["acknowledged_tool_names"])
                        if let value = realtime["model"]?.stringValue, !value.isEmpty { model = value }
                        if let value = realtime["provider_session_id"]?.stringValue, !value.isEmpty {
                            providerSession = value
                        }
                        if let value = realtime["computer_tool_schema_hash"]?.stringValue, !value.isEmpty, value != "null" {
                            schemaHash = value
                        }
                        let wasReady = toolsReady
                        toolsReady = computerSurfaceReady(acknowledged)
                            && (realtime["upstream_session_ready"]?.boolValue ?? true)
                        if toolsReady != wasReady {
                            emit("tools", [
                                "acknowledged": acknowledged,
                                "schema_hash": schemaHash,
                                "tools_ready": toolsReady,
                            ])
                        }
                        maybeSend()
                    }
                } else if event.type == "computer_request" {
                    lastActivity = Date()
                    let command = event.command ?? event.action ?? ""
                    let requestId = event.requestId ?? UUID().uuidString
                    let started = Date()
                    let result = MacControlService.shared.handle(
                        command: command,
                        arguments: event.argumentObject,
                        requestId: requestId
                    )
                    let jpeg = result["jpeg"] as? Data
                    var payload = result
                    payload.removeValue(forKey: "jpeg")
                    connection.sendComputerResult(
                        requestId: requestId,
                        command: command,
                        result: payload,
                        jpeg: jpeg,
                        deviceId: deviceId
                    )
                    let ms = Int(Date().timeIntervalSince(started) * 1000)
                    let row: [String: Any] = [
                        "command": command,
                        "request_id": requestId,
                        "ok": payload["ok"] as? Bool ?? false,
                        "executed": payload["executed"] as? Bool ?? false,
                        "verified": payload["verified"] as? Bool ?? false,
                        "must_continue": payload["must_continue"] as? Bool ?? false,
                        "action": payload["action"] as Any,
                        "player_state": payload["player_state"] as Any,
                        "error": payload["error"] as Any,
                        "url": payload["url"] as Any,
                        "query": payload["query"] as Any,
                        "display": payload["display"] as Any,
                        "body": payload["body"] as Any,
                        "spoken": payload["spoken"] as Any,
                        "method": payload["method"] as Any,
                        "latency_ms": ms,
                    ]
                    toolCalls.append(row)
                    emit("computer_result", row)
                } else if event.type == "reply" || event.type == "final_transcript" {
                    if let text = event.text, !text.isEmpty {
                        if event.type == "reply" {
                            replies.append(text)
                            lastActivity = Date()
                            emit("reply", ["text": text])
                        } else {
                            emit("transcript", ["text": text])
                        }
                    }
                } else if event.fatal {
                    emit("channel_closed", ["code": event.code as Any, "text": event.text as Any])
                    break
                }
                if sentText,
                   !sentFollowUp,
                   followUp != nil,
                   !replies.isEmpty,
                   toolCalls.contains(where: {
                       let action = ($0["action"] as? String) ?? ""
                       let isPlay = action == "play" || action.hasPrefix("play_")
                       return ($0["command"] as? String) == "app_action"
                           && (($0["player_state"] as? String) == "playing" || isPlay)
                           && ($0["verified"] as? Bool) == true
                   })
                {
                    let next = followUp ?? ""
                    connection.sendText(next)
                    sentFollowUp = true
                    lastActivity = Date()
                    emit("follow_up_sent", ["text": next])
                }
                if sentText,
                   !replies.isEmpty,
                   Date().timeIntervalSince(lastActivity) > 8
                {
                    let hasSemantic = toolCalls.contains { computerCommand($0["command"] as? String) }
                    let waitingFollowUp = followUp != nil && !sentFollowUp
                    let actions = toolCalls.compactMap { $0["action"] as? String }
                    var stillGoing = (toolCalls.last?["must_continue"] as? Bool) == true
                    if lowerUtterance.contains("first result") || lowerUtterance.contains("open the first") {
                        stillGoing = stillGoing || !(actions.contains("navigate") || actions.contains("open_item"))
                    }
                    if wantsLaptopFiles {
                        stillGoing = stillGoing || !fileOpSucceeded(toolCalls, utterance: lowerUtterance)
                    }
                    if (!needsComputerAction || hasSemantic) && !waitingFollowUp && !stillGoing {
                        break
                    }
                    if sentFollowUp && hasSemantic && Date().timeIntervalSince(lastActivity) > 10 {
                        break
                    }
                }
            }
            watchdog.cancel()
            idleWatch.cancel()
            connection.close()
            let music = MacControlService.shared.handle(
                command: "app_action",
                arguments: ["app": "Music", "action": "status"],
                requestId: "e2e-music-status"
            )
            emit("music_status", music)
            emit("summary", [
                "session_id": opened.sessionId,
                "provider_session_id": providerSession,
                "model": model,
                "schema_hash": schemaHash,
                "advertised": advertised,
                "acknowledged": acknowledged,
                "tools_ready": toolsReady,
                "utterance": utterance,
                "tool_calls": toolCalls,
                "replies": replies,
                "music_track": music["track"] as Any,
                "music_state": music["player_state"] as Any,
                "music_verified": music["verified"] as Any,
            ])
            let playing = (music["player_state"] as? String) == "playing"
            let ok: Bool
            switch expect {
            case "music-playing":
                ok = playing && toolsReady && sentText
            case "music-stopped":
                ok = toolsReady && sentText && !playing
            case "chrome-search":
                ok = toolsReady && sentText && chromeSearchSucceeded(toolCalls)
            case "notes-written":
                ok = toolsReady && sentText && notesWriteSucceeded(toolCalls)
            case "notes-read":
                ok = toolsReady && sentText && notesReadSucceeded(toolCalls)
            case "safari-search":
                ok = toolsReady && sentText && safariSearchSucceeded(toolCalls)
            case "file-written", "file-read", "file-edited", "file-listed", "file-opened", "file-op":
                ok = toolsReady && sentText && fileOpSucceeded(toolCalls, utterance: lowerUtterance)
            default:
                ok = toolsReady && sentText && (
                    !needsComputerAction
                    || toolCalls.contains { computerCommand($0["command"] as? String) }
                    || fileOpSucceeded(toolCalls, utterance: lowerUtterance)
                )
            }
            if ok {
                emit("pass", ["boundary": "live_e2e", "expect": expect])
                return 0
            }
            emit("fail", [
                "boundary": !toolsReady ? "provider_tools" : "expect",
                "expect": expect,
                "tools_ready": toolsReady,
                "playing": playing,
            ])
            return 1
        } catch {
            emit("fail", ["boundary": "live_connect", "detail": error.localizedDescription])
            return 2
        }
    }

    private static func emit(_ kind: String, _ payload: [String: Any]) {
        var body = payload
        body["event"] = kind
        guard JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body),
              let line = String(data: data, encoding: .utf8)
        else { return }
        print("\(marker) \(line)")
        fflush(stdout)
    }

    private static func computerSurfaceReady(_ names: [String]) -> Bool {
        let set = Set(names)
        if set.contains("computer") {
            return true
        }
        let apps = set.contains("open_app") || set.contains("list_apps") || set.contains("activate_app")
        let inApp = set.contains("app_action")
            || set.contains("inspect_ui")
            || set.contains("read")
            || set.contains("see")
            || set.contains("click")
        return apps && inApp
    }

    private static func computerCommand(_ command: String?) -> Bool {
        let name = command ?? ""
        return [
            "app_action", "inspect_ui", "ui_action", "screen_look",
            "read", "see", "click", "double_click", "right_click",
            "type", "paste", "key", "scroll", "drag", "open_app", "open_url",
            "computer", "file_op",
        ].contains(name)
    }

    private static func chromeSearchSucceeded(_ calls: [[String: Any]]) -> Bool {
        calls.contains { row in
            let url = String(describing: row["url"] ?? "").lowercased()
            let command = row["command"] as? String
            return command == "app_action"
                && (url.contains("google.com/search") || url.contains("openai"))
        }
    }

    private static func notesWriteSucceeded(_ calls: [[String: Any]]) -> Bool {
        calls.contains { row in
            let action = (row["action"] as? String) ?? ""
            let verified = (row["verified"] as? Bool) ?? false
            let command = row["command"] as? String
            return verified && (
                (command == "app_action" && ["create", "append", "replace"].contains(action))
                    || action == "type" || command == "type"
            )
        }
    }

    private static func notesReadSucceeded(_ calls: [[String: Any]]) -> Bool {
        calls.contains { row in
            let action = (row["action"] as? String) ?? ""
            let verified = (row["verified"] as? Bool) ?? false
            let command = row["command"] as? String
            let body = String(describing: row["body"] ?? "")
            let spoken = String(describing: row["spoken"] ?? "").lowercased()
            let text = body.trimmingCharacters(in: .whitespacesAndNewlines)
            return command == "app_action"
                && action == "read"
                && verified
                && text.count > 2
                && !spoken.contains("couldn't read")
        }
    }

    private static func safariSearchSucceeded(_ calls: [[String: Any]]) -> Bool {
        calls.contains { row in
            let url = String(describing: row["url"] ?? "").lowercased()
            let query = String(describing: row["query"] ?? "").lowercased()
            let command = row["command"] as? String
            let action = (row["action"] as? String) ?? ""
            let verified = (row["verified"] as? Bool) ?? false
            let mustContinue = (row["must_continue"] as? Bool) ?? true
            return command == "app_action"
                && action == "search"
                && verified
                && !mustContinue
                && url.contains("google.com/search")
                && !query.contains("result in safari")
                && query.count < 80
        }
    }

    private static func fileUtterance(_ text: String) -> Bool {
        let hasFileCue = text.contains("desktop")
            || text.contains("documents")
            || text.contains("downloads")
            || text.contains(".txt")
            || text.contains(".html")
            || text.contains("local file")
        let hasVerb = text.contains("write")
            || text.contains("read")
            || text.contains("edit")
            || text.contains("list")
            || text.contains("open")
            || text.contains("create")
            || text.contains("the files")
        return hasFileCue && hasVerb
    }

    private static func fileOpSucceeded(_ calls: [[String: Any]], utterance: String) -> Bool {
        let wanted: String
        if utterance.contains("list") || utterance.contains("the files") || utterance.contains("what's on") {
            wanted = "list"
        } else if utterance.contains("edit") || utterance.contains("change") || utterance.contains("replace") {
            wanted = "edit"
        } else if utterance.contains("read") || utterance.contains("what's in") || utterance.contains("what is in") {
            wanted = "read"
        } else if utterance.contains("open") {
            wanted = "open"
        } else {
            wanted = "write"
        }
        return calls.contains { row in
            let command = row["command"] as? String
            let action = (row["action"] as? String) ?? ""
            let verified = (row["verified"] as? Bool) ?? false
            let ok = (row["ok"] as? Bool) ?? false
            guard command == "file_op", verified, ok else { return false }
            if wanted == "edit" {
                return action == "edit" || action == "write"
            }
            return action == wanted || action.isEmpty
        }
    }

    private static func e2eBearerToken(_ fallback: String) -> String {
        let env = ProcessInfo.processInfo.environment
        if let value = env["EV_API_KEY"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           value.count >= 16,
           !["dev", "changeme", "secret", "placeholder"].contains(value.lowercased()) {
            return value
        }
        return fallback
    }

    private static func stringArg(_ args: [String], _ name: String) -> String? {
        guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
        return args[index + 1]
    }

    private static func stringList(_ value: AnyCodable?) -> [String] {
        guard let value else { return [] }
        if let list = value.arrayValue {
            return list.compactMap { $0.stringValue }
        }
        if let text = value.stringValue {
            return text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        }
        return []
    }
}
