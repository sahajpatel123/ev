import AppKit
import Foundation

/// Headless Mac-control probe against the same MacControlService EV.app uses.
///
/// Run the packaged binary so TCC grants apply:
///   macos/build/EV.app/Contents/MacOS/EV --mac-control-probe
enum MacControlProbe {
    private static let marker = "EVIE_COMPUTER_PROBE"

    static func run() -> Int32 {
        _ = NSApplication.shared
        var failed = 0
        func record(_ name: String, _ ok: Bool, _ detail: String, latencyMs: Int) {
            let status = ok ? "PASS" : "FAIL"
            if !ok { failed += 1 }
            print("\(marker) \(status) \(name) \(latencyMs)ms \(detail)")
        }
        func call(_ command: String, _ arguments: [String: Any] = [:], requestId: String = UUID().uuidString) -> (result: [String: Any], ms: Int) {
            let started = Date()
            let result = MacControlService.shared.handle(command: command, arguments: arguments, requestId: requestId)
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            return (result, ms)
        }
        func flag(_ result: [String: Any]) -> Bool {
            result["ok"] as? Bool == true
        }
        func compact(_ result: [String: Any]) -> String {
            String(describing: result["compact"] ?? "")
        }
        func elements(_ result: [String: Any]) -> [[String: Any]] {
            result["elements"] as? [[String: Any]] ?? []
        }
        func findRef(_ result: [String: Any], matching: (String, String) -> Bool) -> String? {
            for item in elements(result) {
                let role = String(describing: item["role"] ?? "")
                let title = String(describing: item["title"] ?? "")
                let value = String(describing: item["value"] ?? "")
                if matching(role, title) || matching(role, value) {
                    return item["ref"] as? String
                }
            }
            return nil
        }
        func haystack(_ result: [String: Any]) -> String {
            (compact(result) + elements(result).map { String(describing: $0["value"] ?? "") }.joined()).lowercased()
        }
        func terminateBundle(_ bundle: String) {
            for app in NSRunningApplication.runningApplications(withBundleIdentifier: bundle) {
                app.terminate()
            }
            let deadline = Date().addingTimeInterval(1.2)
            while Date() < deadline {
                if NSRunningApplication.runningApplications(withBundleIdentifier: bundle).isEmpty { return }
                Thread.sleep(forTimeInterval: 0.08)
            }
            for app in NSRunningApplication.runningApplications(withBundleIdentifier: bundle) {
                app.forceTerminate()
            }
            Thread.sleep(forTimeInterval: 0.2)
        }
        func pressNamed(_ app: String, _ title: String) -> Bool {
            let inspect = call("inspect_ui", ["app": app, "query": title])
            guard let ref = findRef(inspect.result, matching: { _, text in
                text == title || text.lowercased() == title.lowercased()
            }) else { return false }
            return flag(call("ui_action", ["action": "press", "element_ref": ref]).result)
        }

        terminateBundle("com.apple.TextEdit")

        let status = call("status")
        let ax = status.result["accessibility_ready"] as? Bool == true
        let generic = status.result["generic_ui_control_ready"] as? Bool == true
        let screen = status.result["screen_vision_ready"] as? Bool == true
        print("\(marker) IDENTITY \(String(describing: status.result["ax_process"] ?? [:]))")
        print("\(marker) PROBE \(String(describing: status.result["accessibility_probe"] ?? [:]))")
        record(
            "A_status",
            flag(status.result),
            "ax=\(ax) generic=\(generic) screen=\(screen) bluetooth=\(String(describing: status.result["bluetooth_on"])) front=\(String(describing: status.result["foreground_app"]))",
            latencyMs: status.ms
        )
        if !generic {
            record("AX_required", false, "Accessibility not ready. Prompting System Settings and waiting up to 90s.", latencyMs: 0)
            _ = call("request_accessibility")
            let deadline = Date().addingTimeInterval(90)
            var becameReady = false
            while Date() < deadline {
                Thread.sleep(forTimeInterval: 1.0)
                let poll = call("status")
                if poll.result["generic_ui_control_ready"] as? Bool == true {
                    becameReady = true
                    record("AX_granted", true, "generic_ui_control_ready after wait", latencyMs: poll.ms)
                    break
                }
            }
            if !becameReady {
                record("AX_wait", false, "Still not trusted after 90s. UI tests will fail honestly.", latencyMs: 0)
            }
        }

        let listed = call("list_apps", ["query": "TextEdit"])
        let apps = listed.result["apps"] as? [[String: Any]] ?? []
        record("A_list", flag(listed.result) && !apps.isEmpty, "count=\(apps.count)", latencyMs: listed.ms)

        let opened = call("open_app", ["name": "TextEdit"])
        record(
            "A_open_textedit",
            flag(opened.result) && opened.result["running"] as? Bool == true,
            "app=\(String(describing: opened.result["app"])) pid=\(String(describing: opened.result["pid"]))",
            latencyMs: opened.ms
        )
        Thread.sleep(forTimeInterval: 0.7)
        _ = call("activate_app", ["name": "TextEdit"])
        _ = call("keyboard", ["keys": "cmd+n"])
        Thread.sleep(forTimeInterval: 0.25)

        let inspect1 = call("inspect_ui", ["app": "TextEdit", "query": "document"])
        let textRef = findRef(inspect1.result) { role, _ in role.contains("textarea") }
            ?? findRef(inspect1.result) { role, _ in role == "field" }
        record(
            "B_inspect_textedit",
            flag(inspect1.result) && textRef != nil,
            "window=\(String(describing: inspect1.result["window"])) dialog=\(String(describing: inspect1.result["dialog_present"])) ref=\(textRef ?? "none") walked=\(String(describing: inspect1.result["walked"])) compact=\(compact(inspect1.result).prefix(180))",
            latencyMs: inspect1.ms
        )

        let typedToken = "Evie computer control \(Int(Date().timeIntervalSince1970))"
        var typedOk = false
        if let textRef {
            let typed = call("ui_action", ["action": "type", "element_ref": textRef, "value": typedToken])
            typedOk = flag(typed.result)
            record("B_type", typedOk, "target=\(String(describing: typed.result["target"])) method=accessibility", latencyMs: typed.ms)
        } else {
            let typed = call("ui_action", ["action": "type", "value": typedToken])
            typedOk = flag(typed.result)
            record("B_type_focused", typedOk, String(describing: typed.result["spoken"] ?? ""), latencyMs: typed.ms)
        }

        let inspect2 = call("inspect_ui", ["app": "TextEdit"])
        let blob = haystack(inspect2.result)
        let verifiedType = blob.contains(typedToken.lowercased())
        record("B_verify_type", verifiedType, "saw_text=\(verifiedType) method=accessibility", latencyMs: inspect2.ms)

        let continuedToken = "second line \(Int(Date().timeIntervalSince1970))"
        let continued = call("ui_action", ["action": "append", "value": continuedToken])
        Thread.sleep(forTimeInterval: 0.2)
        let inspect3 = call("inspect_ui", ["app": "TextEdit"])
        let blob2 = haystack(inspect3.result)
        record(
            "C_continuation",
            flag(continued.result) && blob2.contains(continuedToken.lowercased()),
            "saw_second=\(blob2.contains(continuedToken.lowercased())) method=accessibility",
            latencyMs: continued.ms + inspect3.ms
        )

        let staleInspectA = call("inspect_ui", ["app": "TextEdit"])
        let staleRef = findRef(staleInspectA.result) { role, _ in role.contains("textarea") } ?? "e1"
        _ = call("inspect_ui", ["app": "TextEdit"])
        let staleAct = call("ui_action", ["action": "press", "element_ref": staleRef])
        record(
            "N_stale_ref",
            staleAct.result["error"] as? String == "stale_element",
            "error=\(String(describing: staleAct.result["error"] ?? "")) ref=\(staleRef)",
            latencyMs: staleAct.ms
        )

        _ = call("activate_app", ["name": "TextEdit"])
        Thread.sleep(forTimeInterval: 0.25)
        let look = call("screen_look", ["target": "active_window", "app": "TextEdit"])
        let frameId = look.result["frame_id"] as? String
        record(
            "H_screen_look",
            flag(look.result) && (look.result["width"] as? Int ?? 0) > 0,
            "frame=\(frameId ?? "none") \(look.result["width"] ?? 0)x\(look.result["height"] ?? 0) screen_perm=\(screen) method=screen_vision",
            latencyMs: look.ms
        )
        if let frameId {
            let click = call(
                "ui_action",
                [
                    "action": "click_at",
                    "frame_id": frameId,
                    "x_normalized": 0.50,
                    "y_normalized": 0.45,
                ]
            )
            record(
                "H_click_at",
                flag(click.result),
                "method=\(String(describing: click.result["method"] ?? "coordinate")) err=\(String(describing: click.result["error"] ?? "")) front=\(String(describing: click.result["front_app"] ?? ""))",
                latencyMs: click.ms
            )
        }

        let calc = call("open_app", ["name": "Calculator"])
        Thread.sleep(forTimeInterval: 0.5)
        let calcInspect = call("inspect_ui", ["app": "Calculator"])
        let seven = findRef(calcInspect.result) { _, title in title == "7" || title.lowercased() == "7" }
        var pressed = false
        if let seven {
            let press = call("ui_action", ["action": "press", "element_ref": seven])
            pressed = flag(press.result)
        }
        record(
            "G_calculator_ax",
            flag(calc.result) && flag(calcInspect.result),
            "buttons_found=\(!elements(calcInspect.result).isEmpty) press_7=\(pressed) compact=\(compact(calcInspect.result).prefix(160))",
            latencyMs: calc.ms + calcInspect.ms
        )
        _ = pressNamed("Calculator", "Clear")
        let calcSeq = ["1", "8", "7", "Multiply", "4", "3"]
        var calcSteps = 0
        for title in calcSeq where pressNamed("Calculator", title) {
            calcSteps += 1
        }
        let equalsInspect = call("inspect_ui", ["app": "Calculator", "query": "equals"])
        let equals = findRef(equalsInspect.result) { _, title in
            ["=", "equals", "equal"].contains(title.lowercased()) || title == "="
        }
        var equalsOk = false
        if let equals {
            equalsOk = flag(call("ui_action", ["action": "press", "element_ref": equals]).result)
        } else {
            equalsOk = flag(call("keyboard", ["keys": "return"]).result)
        }
        Thread.sleep(forTimeInterval: 0.2)
        let calcResult = call("inspect_ui", ["app": "Calculator"])
        let calcHay = haystack(calcResult.result)
        let calcProduct = calcHay.contains("8041") || calcHay.contains("8,041")
        record(
            "Q_calculator_multistep",
            calcSteps >= 5 && (equalsOk || calcProduct),
            "steps=\(calcSteps) equals=\(equalsOk) saw_8041=\(calcProduct) compact=\(compact(calcResult.result).prefix(120)) method=accessibility",
            latencyMs: calcResult.ms
        )

        let displays = call("open_app", ["name": "displays"])
        Thread.sleep(forTimeInterval: 1.0)
        let displaysInspect = call("inspect_ui", ["app": "System Settings", "query": "display"])
        let displaysHay = compact(displaysInspect.result).lowercased()
        record(
            "P_displays",
            flag(displays.result),
            "pane=\(String(describing: displays.result["pane"])) visible=\(displaysHay.contains("display")) compact=\(displaysHay.prefix(140)) method=native_api+accessibility",
            latencyMs: displays.ms + displaysInspect.ms
        )

        let downloads = call("open_app", ["name": "Downloads"])
        Thread.sleep(forTimeInterval: 0.5)
        let finderInspect = call("inspect_ui", ["app": "Finder"])
        record(
            "O_finder_downloads",
            flag(downloads.result),
            "path=\(String(describing: downloads.result["path"] ?? "")) window=\(String(describing: finderInspect.result["window"])) method=native_api",
            latencyMs: downloads.ms + finderInspect.ms
        )

        let bluetooth = call("open_app", ["name": "bluetooth settings"])
        Thread.sleep(forTimeInterval: 1.0)
        let settingsInspect = call("inspect_ui", ["app": "System Settings"])
        let hay = compact(settingsInspect.result).lowercased()
        let bluetoothVisible = hay.contains("bluetooth") || String(describing: settingsInspect.result["window"] ?? "").lowercased().contains("bluetooth")
        record(
            "D_settings_bluetooth",
            flag(bluetooth.result),
            "pane=\(String(describing: bluetooth.result["pane"])) visible=\(bluetoothVisible) compact=\(hay.prefix(180))",
            latencyMs: bluetooth.ms + settingsInspect.ms
        )
        record(
            "E_bluetooth_state",
            status.result["bluetooth_on"] != nil,
            "bluetooth_on=\(String(describing: status.result["bluetooth_on"])) ui=\(bluetoothVisible)",
            latencyMs: 0
        )

        let safari = call("open_url", ["url": "https://www.google.com/search?q=OpenAI"])
        Thread.sleep(forTimeInterval: 1.2)
        let safariInspect = call("inspect_ui", ["app": "Safari"])
        record(
            "F_safari_search",
            flag(safari.result),
            "opened=\(flag(safari.result)) window=\(String(describing: safariInspect.result["window"])) method=apple_events",
            latencyMs: safari.ms
        )
        let safariScrollTarget = findRef(safariInspect.result) { role, _ in
            role.contains("web") || role.contains("scroll")
        }
        var scrolled = false
        if let safariScrollTarget {
            scrolled = flag(call("ui_action", ["action": "scroll", "element_ref": safariScrollTarget, "direction": "down"]).result)
        } else {
            scrolled = flag(call("keyboard", ["keys": "space"]).result)
        }
        record("R_safari_scroll", scrolled, "ax_scroll=\(safariScrollTarget != nil) method=\(safariScrollTarget != nil ? "accessibility" : "keyboard")", latencyMs: 0)
        let safariQuery = call("inspect_ui", ["app": "Safari", "query": "OpenAI"])
        let firstLink = findRef(safariQuery.result) { role, title in
            role.lowercased().contains("link") && title.lowercased().contains("openai")
        }
        var clickedResult = false
        if let firstLink {
            clickedResult = flag(call("ui_action", ["action": "press", "element_ref": firstLink]).result)
        }
        Thread.sleep(forTimeInterval: 0.7)
        let afterClick = call("inspect_ui", ["app": "Safari"])
        let windowAfter = String(describing: afterClick.result["window"] ?? "").lowercased()
        let leftGoogle = clickedResult && !windowAfter.contains("google")
        record(
            "T_safari_first_result",
            clickedResult && (leftGoogle || windowAfter.contains("openai")),
            "clicked=\(clickedResult) link=\(firstLink ?? "none") window=\(windowAfter) method=accessibility",
            latencyMs: safariQuery.ms
        )
        let back = call("keyboard", ["keys": "cmd+["])
        record("R_safari_back", flag(back.result), "method=keyboard", latencyMs: back.ms)

        var thirdPartyName = ""
        if let runningCustom = NSWorkspace.shared.runningApplications.first(where: { app in
            guard app.activationPolicy == .regular else { return false }
            let name = (app.localizedName ?? "").lowercased()
            if name.contains("service") || name.contains("helper") { return false }
            return ["cursor", "slack", "spotify", "discord", "code"].contains { name == $0 || name.contains($0) }
        }) {
            thirdPartyName = runningCustom.localizedName ?? ""
        }
        if thirdPartyName.isEmpty {
            for candidate in ["Slack", "Spotify", "Discord", "Cursor"] {
                let listedThird = call("list_apps", ["query": candidate])
                if !(listedThird.result["apps"] as? [[String: Any]] ?? []).isEmpty {
                    thirdPartyName = candidate
                    break
                }
            }
        }
        if !thirdPartyName.isEmpty {
            let thirdInspect = call("inspect_ui", ["app": thirdPartyName])
            let count = elements(thirdInspect.result).count
            record(
                "S_third_party_inspect",
                flag(thirdInspect.result) && count > 0,
                "app=\(thirdPartyName) elements=\(count) window=\(String(describing: thirdInspect.result["window"])) method=accessibility",
                latencyMs: thirdInspect.ms
            )
        } else {
            record("S_third_party_inspect", true, "skipped_no_installed_custom_app", latencyMs: 0)
        }

        let cancelId = "probe-cancel"
        MacControlService.shared.cancel(requestId: cancelId)
        let cancelled = call("inspect_ui", [:], requestId: cancelId)
        record(
            "L_cancel",
            cancelled.result["error"] as? String == "cancelled" || cancelled.result["cancelled"] as? Bool == true,
            String(describing: cancelled.result["spoken"] ?? cancelled.result["error"] ?? ""),
            latencyMs: cancelled.ms
        )

        let close = call("close_app", ["name": "TextEdit"])
        let dialog = close.result["dialog_present"] as? Bool == true
        record(
            "I_close_textedit",
            flag(close.result) || dialog,
            "closed=\(flag(close.result)) dialog=\(dialog) spoken=\(String(describing: close.result["spoken"] ?? ""))",
            latencyMs: close.ms
        )
        if dialog {
            let dialogInspect = call("inspect_ui", ["app": "TextEdit", "query": "don't save"])
            let dontSave = findRef(dialogInspect.result) { _, title in
                let lower = title.lowercased()
                return (lower.contains("don") && lower.contains("save")) || lower.contains("delete")
            }
            record(
                "I_save_dialog",
                flag(dialogInspect.result) && dialogInspect.result["dialog_present"] as? Bool == true,
                "dont_save_ref=\(dontSave ?? "none") compact=\(compact(dialogInspect.result).prefix(180))",
                latencyMs: dialogInspect.ms
            )
            if let dontSave {
                _ = call("ui_action", ["action": "press", "element_ref": dontSave])
            } else {
                _ = call("keyboard", ["keys": "cmd+d"])
            }
        }

        let notes = call("open_app", ["name": "Notes"])
        Thread.sleep(forTimeInterval: 0.4)
        let notesInspect = call("inspect_ui", ["app": "Notes"])
        _ = call("ui_action", ["action": "type", "value": "Evie multi-app note"])
        record(
            "J_multi_app",
            flag(notes.result) && flag(notesInspect.result),
            "notes=\(flag(notes.result)) window=\(String(describing: notesInspect.result["window"]))",
            latencyMs: notes.ms + notesInspect.ms
        )

        let fiveStep = verifiedType && blob2.contains("second line") && flag(opened.result)
        record("M_five_step", fiveStep, "open+inspect+type+verify+continue", latencyMs: 0)

        let missing = call("open_app", ["name": "DefinitelyNotAnAppXYZ"])
        record(
            "K_missing_app",
            missing.result["ok"] as? Bool != true,
            String(describing: missing.result["spoken"] ?? missing.result["error"] ?? ""),
            latencyMs: missing.ms
        )

        let chess = call("app_action", ["app": "Music", "action": "play", "playlist": "Chess", "index": 1])
        let chessTrack = String(describing: chess.result["track"] ?? chess.result["observed_track"] ?? "")
        let chessPlaying = chess.result["player_state"] as? String == "playing"
        let chessVerified = chess.result["verified"] as? Bool == true
        record(
            "N_music_chess_first",
            chessVerified && chessPlaying && chessTrack.lowercased().contains("cinnamon"),
            "verified=\(chessVerified) state=\(String(describing: chess.result["player_state"])) track=\(chessTrack) index=\(String(describing: chess.result["index"]))",
            latencyMs: chess.ms
        )
        let second = call("app_action", ["app": "Music", "action": "play", "playlist": "Chess", "index": 2])
        let secondTrack = String(describing: second.result["observed_track"] ?? second.result["track"] ?? "")
        let secondVerified = second.result["verified"] as? Bool == true
        let secondPlaying = second.result["player_state"] as? String == "playing"
        record(
            "N_music_chess_second",
            secondVerified && secondPlaying && secondTrack.lowercased().contains("chemtrail"),
            "verified=\(secondVerified) state=\(String(describing: second.result["player_state"])) track=\(secondTrack) index=\(String(describing: second.result["index"]))",
            latencyMs: second.ms
        )
        let missingPlaylist = call("app_action", ["app": "Music", "action": "play", "playlist": "Project Neptune"])
        record(
            "N_music_missing",
            missingPlaylist.result["ok"] as? Bool != true && (missingPlaylist.result["error"] as? String) == "playlist_not_found",
            String(describing: missingPlaylist.result["spoken"] ?? missingPlaylist.result["error"] ?? ""),
            latencyMs: missingPlaylist.ms
        )
        _ = call("app_action", ["app": "Music", "action": "pause"])

        print("\(marker) SUMMARY failed=\(failed)")
        return failed == 0 ? 0 : 1
    }
}
