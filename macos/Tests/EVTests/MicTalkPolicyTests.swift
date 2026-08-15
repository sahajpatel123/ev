import AppKit
import AVFoundation
import Darwin
import EVRuntime
import Foundation

/// Drives the shipped permission-status, Talk-routing, and terminate-policy
/// functions from their real start state.
///
/// Command Line Tools here have no XCTest. This executable is the in-repo
/// test: `swift run --package-path macos EVMicTalkTests`.
@main
enum EVMicTalkTests {
    static func main() {
        var failed = 0
        func check(_ name: String, _ condition: @autoclosure () -> Bool, _ detail: String = "") {
            if condition() {
                print("PASS \(name)")
            } else {
                failed += 1
                print("FAIL \(name)\(detail.isEmpty ? "" : " — \(detail)")")
            }
        }

        TerminatePolicy.explicitQuit = false
        MicrophoneAuthorization.resetForTests()
        AudioInputLease.resetForTests()

        // MARK: Criterion 1 — authorized mic is granted, never off/denied
        let authorized = MicrophoneAuthorization.state(authorizationStatus: .authorized)
        check("authorized-is-granted", authorized == .granted)
        check("authorized-not-off-or-denied", !authorized.isOffOrDenied && authorized.isUsable)
        check("authorized-raw-not-off", authorized.rawValue != "off" && authorized.rawValue != "denied")

        let justAccepted = MicrophoneAuthorization.state(
            authorizationStatus: .notDetermined,
            requestJustGranted: true
        )
        check("just-accepted-prompt-is-granted", justAccepted == .granted && !justAccepted.isOffOrDenied)

        let staleDenied = MicrophoneAuthorization.state(
            authorizationStatus: .denied,
            requestJustGranted: true
        )
        check("just-accepted-stale-denied-is-granted", staleDenied == .granted && !staleDenied.isOffOrDenied)

        let stillRestricted = MicrophoneAuthorization.state(
            authorizationStatus: .restricted,
            requestJustGranted: true
        )
        check("just-accepted-restricted-stays-restricted", stillRestricted == .restricted && stillRestricted.isOffOrDenied)

        let audioApp = MicrophoneAuthorization.state(
            authorizationStatus: .notDetermined,
            audioRecordPermissionGranted: true
        )
        check("audio-app-granted-wins-stale-capture", audioApp == .granted && !audioApp.isOffOrDenied)

        let tcc = AVCaptureDevice.authorizationStatus(for: .audio)
        let livePresented = MicrophoneAuthorization.state(authorizationStatus: tcc)
        if tcc == .authorized {
            check("live-tcc-authorized-is-granted", livePresented == .granted && !livePresented.isOffOrDenied)
            let current = MicrophoneAuthorization.current()
            check("current-authorized-is-granted", current == .granted && !current.isOffOrDenied)
        } else {
            print("INFO live-tcc-status=\(String(describing: tcc)) presented=\(livePresented.rawValue)")
        }
        check(
            "mapping-authorized-always-granted",
            MicrophoneAuthorization.state(authorizationStatus: .authorized) == .granted
        )
        check(
            "denied-is-off-or-denied",
            MicrophoneAuthorization.state(authorizationStatus: .denied).isOffOrDenied
        )
        check(
            "restricted-is-off-or-denied",
            MicrophoneAuthorization.state(authorizationStatus: .restricted).isOffOrDenied
        )

        // MARK: Criterion 2 — Talk while live owns mic does not start clip
        check(
            "live-owns-when-active",
            TalkRouting.liveOwnsInput(isLiveActive: true, isLiveMuted: false, liveIsRunning: true)
        )
        let talkLive = TalkRouting.action(liveOwnsInput: true, isRecording: false, sendingVoice: false)
        check("talk-while-live-toggles-mute", talkLive == .toggleLiveMute)
        check("talk-while-live-does-not-start-clip", talkLive != .startClipCapture)

        check(
            "live-owns-when-muted",
            TalkRouting.liveOwnsInput(isLiveActive: false, isLiveMuted: true, liveIsRunning: true)
        )
        let talkMuted = TalkRouting.action(liveOwnsInput: true, isRecording: true, sendingVoice: false)
        check("talk-while-muted-toggles-mute", talkMuted == .toggleLiveMute)
        check("talk-while-muted-does-not-start-or-stop-clip", talkMuted != .startClipCapture && talkMuted != .stopClipCapture)

        check(
            "live-does-not-own-when-idle",
            !TalkRouting.liveOwnsInput(isLiveActive: false, isLiveMuted: false, liveIsRunning: false)
        )
        check(
            "talk-without-live-starts-clip",
            TalkRouting.action(liveOwnsInput: false, isRecording: false, sendingVoice: false) == .startClipCapture
        )
        check(
            "talk-while-recording-stops-clip",
            TalkRouting.action(liveOwnsInput: false, isRecording: true, sendingVoice: false) == .stopClipCapture
        )
        check(
            "talk-while-sending-ignores",
            TalkRouting.action(liveOwnsInput: false, isRecording: false, sendingVoice: true) == .ignore
        )

        check("lease-acquire-live", AudioInputLease.acquire(.live))
        check("lease-owner-is-live", AudioInputLease.currentOwner() == .live)
        check("lease-clip-blocked-while-live", !AudioInputLease.acquire(.clip))
        check("lease-still-live-after-blocked-clip", AudioInputLease.currentOwner() == .live)
        AudioInputLease.release(.live)
        check("lease-released", AudioInputLease.currentOwner() == nil)
        check("lease-acquire-clip", AudioInputLease.acquire(.clip))
        check("lease-live-blocked-while-clip", !AudioInputLease.acquire(.live))
        AudioInputLease.release(.clip)

        do {
            try ObjCException.raiseAndCatchForTests()
            check("objc-exception-is-caught", false, "exception escaped")
        } catch {
            check(
                "objc-exception-is-caught",
                error.localizedDescription.contains("guard-me")
            )
        }

        // MARK: Criterion 3 — last-window / non-Quit is not a quit
        check("last-window-closed-is-not-quit", TerminatePolicy.shouldTerminateAfterLastWindowClosed == false)
        TerminatePolicy.explicitQuit = false
        check("terminate-cancels-without-quit", !TerminatePolicy.allowsTerminate() && TerminatePolicy.reply() == .terminateCancel)
        TerminatePolicy.markExplicitQuit()
        check("terminate-now-on-explicit-quit", TerminatePolicy.allowsTerminate() && TerminatePolicy.reply() == .terminateNow)
        TerminatePolicy.explicitQuit = false

        // MARK: Shipped wiring
        do {
            let root = macosRoot()
            let appModel = try read(root.appendingPathComponent("Sources/EV/AppModel.swift"))
            check("wired-AppModel-TalkRouting.action", appModel.contains("TalkRouting.action"))
            check("wired-AppModel-TalkRouting.liveOwnsInput", appModel.contains("TalkRouting.liveOwnsInput"))
            check("wired-AppModel-mic-didChange", appModel.contains("MicrophoneAuthorization.didChange"))
            check("wired-AppModel-hotkey-at-start", appModel.contains("hotkey.start"))
            check(
                "wired-AppModel-recheck-live-after-clip-start",
                appModel.range(of: "let started = await mic.start()") != nil
                    && appModel.contains("if TalkRouting.liveOwnsInput")
            )

            let hotkey = try read(root.appendingPathComponent("Sources/EV/GlobalHotkey.swift"))
            check("wired-GlobalHotkey-local-monitor", hotkey.contains("addLocalMonitorForEvents"))
            check("wired-GlobalHotkey-global-monitor", hotkey.contains("addGlobalMonitorForEvents"))

            let menu = try read(root.appendingPathComponent("Sources/EV/MenuBarView.swift"))
            check("wired-MenuBarView-toggleTalk", menu.contains("model.toggleTalk()"))
            check("wired-MenuBarView-no-late-hotkey", !menu.contains("hotkey.start"))

            let live = try read(root.appendingPathComponent("Sources/EV/LiveConversation.swift"))
            check("wired-LiveConversation-requestAccess", live.contains("MicrophoneAuthorization.requestAccess"))
            check("wired-LiveConversation-live-lease", live.contains("AudioInputLease.acquire(.live)"))
            check("wired-LiveConversation-ears-stopAndWait", live.contains("EarsProcess.stopAndWait()"))

            let mic = try read(root.appendingPathComponent("Sources/EV/MicCapture.swift"))
            check("wired-MicCapture-requestAccess", mic.contains("MicrophoneAuthorization.requestAccess"))
            check("wired-MicCapture-clip-lease", mic.contains("AudioInputLease.acquire(.clip)"))
            check("wired-MicCapture-ObjCException", mic.contains("ObjCException.attachAndPrepare"))

            let permissions = try read(root.appendingPathComponent("Sources/EV/PermissionCenter.swift"))
            check("wired-PermissionCenter-current", permissions.contains("MicrophoneAuthorization.current()"))
            check("wired-PermissionCenter-requestAccess", permissions.contains("MicrophoneAuthorization.requestAccess"))
            check("wired-PermissionCenter-notificationCenter", permissions.contains("ObjCException.notificationCenter"))
            check(
                "wired-PermissionsPanel-didChange",
                permissions.contains("MicrophoneAuthorization.didChange")
            )

            let app = try read(root.appendingPathComponent("Sources/EV/EVApp.swift"))
            check("wired-EVApp-last-window", app.contains("TerminatePolicy.shouldTerminateAfterLastWindowClosed"))
            check("wired-EVApp-reply", app.contains("TerminatePolicy.reply()"))

            let application = try read(root.appendingPathComponent("Sources/EV/EVApplication.swift"))
            check("wired-EVApplication-allowsTerminate", application.contains("TerminatePolicy.allowsTerminate"))

            let lifecycle = try read(root.appendingPathComponent("Sources/EV/AppLifecycle.swift"))
            check("wired-AppLifecycle-markExplicitQuit", lifecycle.contains("TerminatePolicy.markExplicitQuit"))

            let plist = try read(root.appendingPathComponent("Resources/Info.plist"))
            check("wired-Info-EVApplication", plist.contains("EVApplication"))
            check("wired-Info-not-NSApplication", !plist.contains("<string>NSApplication</string>"))

            let liveMic = try read(
                root.deletingLastPathComponent()
                    .appendingPathComponent("ios/EVClient/Sources/EVClient/LiveVoice.swift")
            )
            check("wired-LiveVoiceMicrophone-tapInstalled", liveMic.contains("tapInstalled"))
            check("wired-LiveVoiceMicrophone-removeTapIfNeeded", liveMic.contains("removeTapIfNeeded"))
            check("wired-LiveVoiceMicrophone-AVAudioSafe", liveMic.contains("AVAudioSafe.attachAndPrepare"))
        } catch {
            failed += 1
            print("FAIL shipped-wiring-read — \(error)")
        }

        if failed == 0 {
            print("EVMicTalkTests: all passed")
        } else {
            print("EVMicTalkTests: \(failed) failed")
        }
        exit(failed == 0 ? 0 : 1)
    }

    private static func macosRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private static func read(_ url: URL) throws -> String {
        try String(contentsOf: url, encoding: .utf8)
    }
}
