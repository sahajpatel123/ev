import AppKit
import AVFoundation
import Darwin
import EVClient
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

        // MARK: Live client protocol — empty capability state and HUD lifecycle
        let emptyManifest = CapabilityManifest(json: [:])
        check("live-empty-manifest-is-empty", emptyManifest.isEmpty)
        check(
            "live-empty-manifest-has-no-capability-buckets",
            emptyManifest.enabled.isEmpty
                && emptyManifest.needsPermission.isEmpty
                && emptyManifest.unavailable.isEmpty
                && emptyManifest.refused.isEmpty
        )

        let setupManifest = CapabilityManifest(
            unavailable: ["Calendar"],
            providers: ["pipeline"],
            requiresConfirmation: ["place_call"]
        )
        check(
            "live-empty-enabled-manifest-renders-setup-state",
            setupManifest.enabled.isEmpty
                && !setupManifest.isEmpty
                && setupManifest.unavailable == ["Calendar"]
        )

        let progress = HUDCard(
            generatedAt: "2026-08-17T00:00:00Z",
            title: "In progress",
            body: "Working on search memory.",
            meta: [
                "kind": .string("progress"),
                "tool": .string("search_memory"),
            ]
        )
        check("live-progress-hud-kind", progress.metaKind == "progress")

        let hold = HUDCard(
            generatedAt: "2026-08-17T00:00:00Z",
            title: "Confirm on this device",
            body: "I’m holding this action for confirmation.",
            meta: [
                "kind": .string("approval_hold"),
                "tool": .string("place_call"),
                "arguments": .object(["name": .string("Ned")]),
            ]
        )
        check("live-confirmation-hold-is-actionable", hold.isApprovalHold)
        check("live-confirmation-hold-tool", hold.holdToolName == "place_call")
        check("live-confirmation-hold-arguments", hold.holdArguments["name"] as? String == "Ned")

        let evidence = HUDCard(
            generatedAt: "2026-08-17T00:00:00Z",
            title: "Place call",
            body: "Called Ned.",
            meta: [
                "kind": .string("evidence"),
                "tool": .string("place_call"),
                "source": .string("phone"),
            ]
        )
        check("live-evidence-hud-kind", evidence.metaKind == "evidence")

        // MARK: Live runtime proof — no stale tools, no secrets
        var runtime = LiveRuntimeDiagnostics()
        runtime.setBackend(
            url: URL(string: "https://owner:secret@example.test/v1?token=hidden")!,
            source: "process environment EV_API_URL"
        )
        runtime.beginConnectionAttempt(
            backendURL: URL(string: "https://owner:secret@example.test/v1?token=hidden")!,
            backendSource: "process environment EV_API_URL",
            deviceID: "mac-runtime-3"
        )
        runtime.updateRuntime(
            provider: "openai",
            model: "gpt-realtime-test",
            advertisedTools: [],
            providerAcknowledgedTools: [],
            providerSessionReady: true,
            capabilityErrors: ["projection unavailable"]
        )
        let emptyRuntimeText = runtime.displayText
        check("live-runtime-backend-url-is-sanitized", !emptyRuntimeText.contains("secret") && !emptyRuntimeText.contains("hidden"))
        check("live-runtime-config-source-is-visible", emptyRuntimeText.contains("process environment EV_API_URL"))
        check("live-runtime-provider-model-visible", emptyRuntimeText.contains("openai") && emptyRuntimeText.contains("gpt-realtime-test"))
        check("live-runtime-empty-tools-visible", emptyRuntimeText.contains("tools advertised: none") && emptyRuntimeText.contains("tools acknowledged: none"))
        check("live-runtime-capability-error-visible", emptyRuntimeText.contains("projection unavailable"))
        check("live-runtime-device-visible", emptyRuntimeText.contains("mac-runtime-3"))

        runtime.recordToolCall(
            LiveRuntimeToolCall(
                name: "set_reminder",
                callID: "call-3",
                argumentKeys: ["when"],
                observedAt: "2026-08-17T00:00:00Z"
            )
        )
        runtime.recordToolResult(
            LiveRuntimeToolResult(
                name: "set_reminder",
                success: true,
                verified: true,
                summary: "",
                observedAt: "2026-08-17T00:00:01Z"
            )
        )
        runtime.recordEvidence(
            LiveRuntimeEvidence(
                source: "calendar",
                timestamp: "2026-08-17T00:00:01Z"
            )
        )
        let toolRuntimeText = runtime.displayText
        check("live-runtime-tool-call-visible", toolRuntimeText.contains("set_reminder") && toolRuntimeText.contains("call-3"))
        check("live-runtime-tool-result-visible", toolRuntimeText.contains("verified"))
        check("live-runtime-evidence-timestamp-visible", toolRuntimeText.contains("calendar") && toolRuntimeText.contains("2026-08-17T00:00:01Z"))

        runtime.beginConnectionAttempt(
            backendURL: URL(string: "http://127.0.0.1:8000")!,
            backendSource: "built-in default",
            deviceID: "mac-runtime-4"
        )
        check("live-runtime-reconnect-clears-stale-tools", runtime.advertisedTools.isEmpty && runtime.providerAcknowledgedTools.isEmpty)
        check("live-runtime-reconnect-updates-device", runtime.deviceID == "mac-runtime-4" && runtime.reconnectCount == 1)

        // MARK: Presence orb — placement under the clock, RMS from PCM
        let size = VoicePresenceMath.panelSize(visibleWidth: 1440)
        check("orb-desktop-width", abs(size.width - 280) < 0.01)
        check("orb-reference-aspect", abs(size.width / size.height - 1504.0 / 1200.0) < 0.001)
        let origin = VoicePresenceMath.overlayOrigin(
            visibleX: 0,
            visibleY: 0,
            visibleWidth: 1440,
            visibleHeight: 875,
            sizeWidth: size.width,
            sizeHeight: size.height
        )
        check("orb-origin-right", abs(origin.x - (1440 - size.width - 8)) < 0.01)
        check("orb-origin-under-menu-bar", abs(origin.y - (875 - size.height - 8)) < 0.01)
        check("orb-origin-not-over-clock", origin.y + size.height <= 875 - 8 + 0.01)
        check(
            "orb-speech-speaking",
            VoicePresenceMath.speechStatus(forAppStatus: "speaking") == .speaking
        )
        check(
            "orb-speech-thinking-prepares",
            VoicePresenceMath.speechStatus(forAppStatus: "thinking") == .preparing
        )
        check(
            "orb-speech-listening-visible",
            VoicePresenceMath.speechStatus(forAppStatus: "listening") == .speaking
        )
        check(
            "orb-speech-offline-hidden",
            VoicePresenceMath.speechStatus(forAppStatus: "offline") == .hidden
        )
        check(
            "orb-audio-only-while-speaking",
            VoicePresenceMath.speechAudioLevel(status: .speaking, output: 0.4) == 0.4
                && VoicePresenceMath.speechAudioLevel(status: .preparing, output: 0.9) == 0
        )
        let compact = VoicePresenceMath.panelSize(visibleWidth: 640)
        check("orb-compact-width", abs(compact.width - 168) < 0.01)
        let tablet = VoicePresenceMath.panelSize(visibleWidth: 900)
        check("orb-tablet-width", abs(tablet.width - 220) < 0.01)

        let silent = Data(count: 512)
        check("orb-silent-rms-zero", VoicePresenceMath.pcm16RMS(silent) == 0)
        check("orb-silent-normalized-zero", VoicePresenceMath.normalizeSpeechRMS(0) == 0)
        check("orb-quiet-floor-zero", VoicePresenceMath.normalizeSpeechRMS(80) == 0)

        var loud = Data(count: 256)
        loud.withUnsafeMutableBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<samples.count {
                samples[i] = Int16(8000).littleEndian
            }
        }
        let loudRMS = VoicePresenceMath.pcm16RMS(loud)
        check("orb-loud-rms-positive", loudRMS > 1000)
        let normalized = VoicePresenceMath.normalizeSpeechRMS(loudRMS)
        check("orb-loud-normalized-in-unit", normalized > 0.5 && normalized <= 1)

        let smoothed = VoicePresenceMath.smooth(previous: 0.1, sample: 0.8, attack: 0.32, release: 0.14)
        check("orb-smooth-attack-between", smoothed > 0.1 && smoothed < 0.8)

        do {
            let overlay = try read(macosRoot().appendingPathComponent("Sources/EV/VoiceOrbOverlay.swift"))
            let renderer = try read(macosRoot().appendingPathComponent("Sources/EV/VoiceOrbRenderer.swift"))
            let assets = try read(macosRoot().appendingPathComponent("Sources/EV/VoiceOrbAssets.swift"))
            let package = try read(macosRoot().appendingPathComponent("scripts/package.sh"))
            let appModel = try read(macosRoot().appendingPathComponent("Sources/EV/AppModel.swift"))
            let fm = FileManager.default
            let orbRoot = macosRoot().appendingPathComponent("Resources/orb")
            check("wired-VoiceOrbOverlay-click-through", overlay.contains("ignoresMouseEvents = true"))
            check("wired-VoiceOrbOverlay-under-clock", overlay.contains("overlayOrigin"))
            check("wired-VoiceOrbOverlay-after-launch", overlay.contains("noteAppDidFinishLaunching"))
            check("wired-VoiceOrbOverlay-pump-on-attach", overlay.contains("startPump()") && overlay.contains("observeStatus"))
            check("wired-VoiceOrbOverlay-metal", overlay.contains("VoiceOrbRenderer") && renderer.contains("import Metal"))
            check("wired-VoiceOrbOverlay-transparent", overlay.contains("isOpaque = false") && overlay.contains("backgroundColor = .clear"))
            check("wired-VoiceOrbOverlay-no-webview", !overlay.contains("import WebKit"))
            check("wired-VoiceOrbOverlay-speech-status", overlay.contains("audioLevel:") && overlay.contains("reduceMotion:"))
            check("wired-VoiceOrb-layer-contents", renderer.contains("setFrameImage"))
            check("wired-VoiceOrb-cgimage", renderer.contains("premultipliedImage") && renderer.contains("premultipliedFirst"))
            check("wired-VoiceOrb-no-metal-present", !renderer.contains("currentDrawable") && !renderer.contains("CAMetalLayer"))
            check("wired-VoiceOrb-chroma-key", renderer.contains("YELLOW_RG") && renderer.contains("blueBias") && renderer.contains("ridge"))
            check("wired-VoiceOrb-video-quad", renderer.contains("orbFragment") && renderer.contains("triangleStrip") && !renderer.contains("SphereGeometry"))
            check("wired-VoiceOrb-clear-draw", renderer.contains("ctx.clear(bounds)"))
            check("wired-VoiceOrb-no-fresnel", !renderer.contains("Fresnel") && !renderer.contains("SphereGeometry"))
            check("wired-VoiceOrb-no-audio-glow", !renderer.contains("AUDIO_GLOW"))
            check("wired-VoiceOrb-identity", overlay.contains("VoiceOrbIdentity") && overlay.contains("ORB DEBUG"))
            check("wired-VoiceOrb-synthetic", renderer.contains("EV_ORB_SYNTHETIC") && renderer.contains("magenta"))
            check("wired-VoiceOrb-assets-mp4", assets.contains("filament-orb.mp4"))
            check(
                "wired-VoiceOrb-asset-mp4",
                fm.fileExists(atPath: orbRoot.appendingPathComponent("filament-orb.mp4").path)
            )
            check(
                "wired-VoiceOrb-asset-still",
                fm.fileExists(atPath: orbRoot.appendingPathComponent("filament-orb-still.png").path)
            )
            let stillURL = orbRoot.appendingPathComponent("filament-orb-still.png")
            if let image = NSImage(contentsOf: stillURL),
               let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) {
                let rep = NSBitmapImageRep(cgImage: cg)
                let width = rep.pixelsWide
                let height = rep.pixelsHigh
                func alphaAt(_ x: Int, _ y: Int) -> CGFloat {
                    rep.colorAt(x: x, y: y)?.alphaComponent ?? -1
                }
                check(
                    "wired-VoiceOrb-still-has-alpha",
                    cg.alphaInfo != .none && cg.alphaInfo != .noneSkipLast && cg.alphaInfo != .noneSkipFirst
                )
                check(
                    "wired-VoiceOrb-still-corner-clear",
                    alphaAt(0, 0) == 0 && alphaAt(width - 1, 0) == 0 && alphaAt(0, height - 1) == 0
                )
                check("wired-VoiceOrb-still-center-open", alphaAt(width / 2, height / 2) < 0.07)
            } else {
                check("wired-VoiceOrb-still-readable", false, "could not decode filament-orb-still.png")
            }
            check("wired-VoiceOrb-audio-energy", overlay.contains("VoiceLevelMeter.shared.snapshot"))
            check("wired-package-orb", package.contains("Resources/orb") && package.contains("filament-orb.mp4"))
            check("wired-AppModel-shows-orb", appModel.contains("VoiceOrbOverlay.shared.attach"))
        } catch {
            failed += 1
            print("FAIL orb-wiring-read — \(error)")
        }

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
            let config = try read(root.appendingPathComponent("Sources/EV/AppConfig.swift"))
            let auth = try read(root.appendingPathComponent("Sources/EVAuth/APIAuthKey.swift"))
            let permissions = try read(root.appendingPathComponent("Sources/EV/PermissionCenter.swift"))
            check("wired-AppModel-TalkRouting.action", appModel.contains("TalkRouting.action"))
            check("wired-AppModel-TalkRouting.liveOwnsInput", appModel.contains("TalkRouting.liveOwnsInput"))
            check("wired-AppModel-mic-didChange", appModel.contains("MicrophoneAuthorization.didChange"))
            check("wired-AppModel-hotkey-at-start", appModel.contains("hotkey.start"))
            check("wired-AppModel-health-capability-manifest", appModel.contains("capabilityManifest = health.capabilityManifest"))
            check("wired-AppModel-runtime-device-mesh", appModel.contains("deviceMesh = DeviceMeshSnapshot"))
            check("wired-AppModel-registry-device-create", appModel.contains("ensureRegistryDevice"))
            check("wired-AppModel-registry-device-type", appModel.contains("deviceType: \"mac\""))
            check("wired-AppModel-confirmHudAction", appModel.contains("func confirmHudAction"))
            check("wired-AppModel-approve-action", appModel.contains("approveAction"))
            check("wired-AppModel-issue-reverification", appModel.contains("issueReverification"))
            check("wired-AppModel-hud-during-live", appModel.contains("refreshHUD(force:"))
            check("wired-AppModel-conversationId", appModel.contains("conversationId"))
            check(
                "wired-AppModel-recheck-live-after-clip-start",
                appModel.range(of: "let started = await mic.start()") != nil
                    && appModel.contains("if TalkRouting.liveOwnsInput")
            )

            check("wired-AppConfig-api-url-environment", config.contains("environment[\"EV_API_URL\"]"))
            check("wired-AppConfig-api-url-user-defaults", config.contains("defaults.string(forKey: \"EV_API_URL\")"))
            check("wired-AppConfig-api-env-source", config.contains("Library/Application Support/EV/api.env"))
            check("wired-AppConfig-home-env-source", config.contains("home.appendingPathComponent(\".ev/env\")"))
            check("wired-AppConfig-repo-env-source", config.contains("Code/ev/.env"))
            check("wired-AppConfig-device-identity", config.contains("environment[\"EV_DEVICE_ID\"]"))
            check("wired-AppConfig-auth-resolver", config.contains("APIAuthKey.resolve"))
            check("wired-APIAuthKey-master-key-source", auth.contains("environment[\"EV_MASTER_KEY\"]") && auth.contains("fileValues[\"EV_MASTER_KEY\"]"))
            check("wired-APIAuthKey-rejects-short-placeholders", auth.contains("minimumLength") && auth.contains("changeme"))
            check("wired-APIAuthKey-does-not-use-ears-key", !auth.contains("environment[\"EV_EARS_API_KEY\"]"))
            check("wired-AppModel-life-helper-discovery", appModel.contains("lifeHelperPath") && appModel.contains("EVLifeHelper"))
            check("wired-AppModel-one-shot-connect", appModel.contains("connectGrantedBridges"))
            check("wired-AppModel-auto-connect-granted", appModel.contains("await connectGrantedBridges(from: statuses)"))
            check("wired-AppModel-no-auto-google-oauth", appModel.contains("openCalendarAuthorization: Bool = false"))
            check("wired-AppModel-no-fake-calendar", !appModel.contains("fake local calendar"))
            check("wired-AppModel-calendar-fallback", appModel.contains("I still need Google Calendar connected. I can start that now."))

            let hotkey = try read(root.appendingPathComponent("Sources/EV/GlobalHotkey.swift"))
            check("wired-GlobalHotkey-local-monitor", hotkey.contains("addLocalMonitorForEvents"))
            check("wired-GlobalHotkey-global-monitor", hotkey.contains("addGlobalMonitorForEvents"))

            let menu = try read(root.appendingPathComponent("Sources/EV/MenuBarView.swift"))
            check("wired-MenuBarView-toggleTalk", menu.contains("model.toggleTalk()"))
            check("wired-MenuBarView-no-late-hotkey", !menu.contains("hotkey.start"))
            check("wired-MenuBarView-confirm-hold", menu.contains("model.confirmHudAction()"))
            check("wired-MenuBarView-capability-summary", menu.contains("capabilitySummary"))
            check("wired-MenuBarView-empty-capability-copy", menu.contains("none reported"))
            check("wired-MenuBarView-device-summary", menu.contains("deviceSummary"))
            check("wired-MenuBarView-grant-report", !menu.contains("Backend bridges"))
            check("wired-MenuBarView-one-shot-connect", !menu.contains("Connect granted bridges"))

            let live = try read(root.appendingPathComponent("Sources/EV/LiveConversation.swift"))
            check("wired-LiveConversation-requestAccess", live.contains("MicrophoneAuthorization.requestAccess"))
            check("wired-LiveConversation-live-lease", live.contains("AudioInputLease.acquire(.live)"))
            check("wired-LiveConversation-ears-stopAndWait", live.contains("EarsProcess.stopAndWait()"))
            check("wired-LiveConversation-mute-control", live.contains("sendControl(\"mute\")"))
            check("wired-LiveConversation-resume-control", live.contains("sendControl(\"resume\")"))
            check("wired-LiveConversation-hud-event", live.contains("case \"hud\":"))
            check("wired-LiveConversation-capability-manifest", live.contains("event.capabilityManifest") && live.contains("model.capabilityManifest"))
            check("wired-LiveConversation-realtime-diagnostics", live.contains("event.config[\"brain\"]"))
            check("wired-LiveConversation-long-mute-reconnect", live.contains("timeIntervalSince($0) >= 20"))
            check("wired-LiveConversation-long-mute-restarts-loop", live.contains("tearDownChannel()") && live.contains("start()"))
            check("wired-LiveConversation-conversation-id", live.contains("model.conversationId"))
            check("wired-LiveConversation-registry-device", live.contains("EV_REGISTRY_DEVICE_ID"))
            check("wired-LiveConversation-opens-with-device", live.contains("openLiveVoice(deviceId: deviceId)"))
            check("wired-LiveConversation-capture-only", !live.contains("player.bind(to: engine)"))
            check("wired-LiveConversation-benign-cancel", live.contains("isBenignRealtimeError"))
            check("wired-LiveConversation-no-tts-chunk-speaking", !live.contains("case \"tts_chunk\":\n            model.status = .speaking"))

            let tts = try read(root.appendingPathComponent("Sources/EV/TTSPlayer.swift"))
            check("wired-TTSPlayer-shared-fallback", tts.contains("detachSharedPlayer()"))
            check("wired-TTSPlayer-mixer-volume", tts.contains("mainMixerNode.outputVolume = 1.0"))
            check("wired-TTSPlayer-echo-gate", tts.contains("shouldMuteCapture"))
            check("wired-TTSPlayer-48k-graph", tts.contains("48_000"))
            check("wired-TTSPlayer-streaming-converter", tts.contains(".noDataNow"))
            check("wired-TTSPlayer-always-play", tts.contains("playerNode.play()"))
            check("wired-TTSPlayer-prime-start", tts.contains("primeStreamPlayback(generation: generation)"))
            check("wired-TTSPlayer-drain-watchdog", tts.contains("noteAssistantAudioComplete()"))
            check("wired-TTSPlayer-no-lead-chop", !tts.contains("return try schedulePCM(pcm, sampleRate: sampleRate)"))
            check("wired-TTSPlayer-no-overrun-reset", !tts.contains("maxStreamLead"))
            check("wired-TTSPlayer-mute-queued-not-node", tts.contains("Do not use `AVAudioPlayerNode.isPlaying`"))
            check("wired-TTSPlayer-playback-reference", tts.contains("playbackSnapshot()"))
            check("wired-TTSPlayer-stop-for-barge-in", tts.contains("stopForBargeIn()"))
            check("wired-LiveConversation-barge-in-session", live.contains("LiveBargeInSession"))
            check("wired-LiveConversation-barge-runtime-marker", live.contains("ev-barge-runtime-v2"))
            check("wired-LiveConversation-barge-trace", live.contains("BargeInTrace.marker()"))
            check("wired-LiveConversation-listen-while-speaking", live.contains("handleMicFrame"))
            check("wired-LiveConversation-stop-first", live.contains("stopForBargeIn()"))
            // P0 regression 2026-08-21: a synchronous stopForBargeIn() inside
            // the mic-tap interrupt closure deadlocked the engine messenger
            // queue and killed EV.app the moment assistant speech began
            // (EV-2026-08-21-*.ips). The stop must be dispatched onto the
            // barge-in control queue, never run on the tap thread.
            check("wired-LiveConversation-barge-control-queue", live.contains("ev.live.barge-in-control"))
            check("wired-LiveConversation-barge-hop", live.contains("controlQueue.async"))
            check(
                "wired-LiveConversation-no-render-thread-stop",
                suffix(live, from: "interrupt: { event in")
                    .map { body -> Bool in
                        guard let hopAt = body.range(of: "controlQueue.async") else { return false }
                        let beforeHop = body[..<hopAt.lowerBound]
                        return !beforeHop.contains("stopForBargeIn()")
                            && body.contains("stopForBargeIn()")
                    } == true,
                "interrupt closure must hop to the barge-in control queue before touching the player"
            )
            let smoke = try read(root.appendingPathComponent("Sources/EV/SmokeTest.swift"))
            check(
                "wired-BargeInProbe-no-render-thread-stop",
                suffix(smoke, from: "interrupt: { event in")
                    .map { $0.contains("DispatchQueue") } == true,
                "probe interrupt must not stop the player on the tap thread"
            )
            check("wired-LiveConversation-preroll-forward", live.contains("event.preroll"))
            check("wired-LiveConversation-drain-watchdog", live.contains("noteAssistantAudioComplete()"))
            check("wired-LiveConversation-computer-request", live.contains("case \"computer_request\""))
            check("wired-LiveConversation-mac-control", live.contains("MacControlService.shared.handle"))
            let mainSwift = try read(root.appendingPathComponent("Sources/EV/main.swift"))
            check("wired-LiveConversation-mac-control-probe", mainSwift.contains("--mac-control-probe"))
            check("wired-LiveConversation-computer-state", live.contains("sendComputerState"))
            check("wired-LiveConversation-no-transcript-chop", !live.contains("prepareForNewTurn"))
            check("wired-LiveConversation-mic-recover", live.contains("microphone.recover()"))

            // ---- Listener presence round-two wiring (directive invariants) ----
            // ONE BACKCHANNEL AUTHORITY: the Mac ALWAYS stands the server
            // cadence lane down — even when local flags are off, because a
            // disabled local engine means SILENCE, not a second authority.
            // (P0 2026-08-22 evening: with flags off, server cues "Okay." /
            // "Yeah." played over Evie's own replies.)
            check(
                "wired-listener-server-lane-always-down",
                live.contains("connection.sendControl(\"listener_presence\"")
                    && !live.contains("if listenerPresence.flags.enabled {"),
                "server backchannel stand-down must be unconditional on Mac"
            )
            let controllerSource = try read(root.appendingPathComponent("Sources/EV/ListenerPresenceController.swift")) ?? ""
            // A7: a nod can never raise assistantSpeaking — the aux enqueue
            // path must not touch onPlayingChange/notifyPlaying.
            check(
                "wired-listener-aux-never-assistant-speaking",
                suffix(tts, from: "func enqueueListenerFeedback")
                    .map { body -> Bool in
                        guard let end = body.range(of: "private func scheduleAuxiliary") else { return false }
                        return !body[..<end.lowerBound].contains("notifyPlaying")
                            && !body[..<end.lowerBound].contains("onPlayingChange")
                    } == true,
                "enqueueListenerFeedback must never raise onPlayingChange (assistantSpeaking stays false)"
            )
            // A8: barge-in stop is ROLE C ONLY — it cannot tear down the aux lane.
            check(
                "wired-listener-stop-for-barge-in-aux-immune",
                tts.contains("stopForBargeIn() {\n        stop(echoTail: false, auxTeardown: false"),
                "stopForBargeIn must pass auxTeardown:false (completion immunity)"
            )
            // Role + completion policy are DECLARED, never inferred.
            check(
                "wired-listener-role-declared",
                controllerSource.contains("role: .listenerBackchannel")
                    && controllerSource.contains("completionPolicy: .finishDespiteOwnerSpeech"),
                "listener nods must declare playback role + completion policy explicitly"
            )
            check(
                "wired-listener-normal-response-preempts",
                live.contains("preemptListenerFeedbackForResponse(reason: \"normal_response_start\")"),
                "NORMAL_RESPONSE > LISTENER_BACKCHANNEL preemption must be wired on tts_chunk"
            )

            let mic = try read(root.appendingPathComponent("Sources/EV/MicCapture.swift"))
            check("wired-MicCapture-requestAccess", mic.contains("MicrophoneAuthorization.requestAccess"))
            check("wired-MicCapture-clip-lease", mic.contains("AudioInputLease.acquire(.clip)"))
            check("wired-MicCapture-ObjCException", mic.contains("ObjCException.attachAndPrepare"))

            check("wired-PermissionCenter-current", permissions.contains("MicrophoneAuthorization.current()"))
            check("wired-PermissionCenter-requestAccess", permissions.contains("MicrophoneAuthorization.requestAccess"))
            check("wired-PermissionCenter-notificationCenter", permissions.contains("ObjCException.notificationCenter"))
            check(
                "wired-PermissionsPanel-didChange",
                permissions.contains("MicrophoneAuthorization.didChange")
            )

            let app = try read(root.appendingPathComponent("Sources/EV/EVApp.swift"))
            check("wired-EVApp-last-window", app.contains("TerminatePolicy.shouldTerminateAfterLastWindowClosed"))
            check("wired-EVApp-orb-after-launch", app.contains("noteAppDidFinishLaunching"))
            check("wired-EVApp-reply", app.contains("TerminatePolicy.reply()"))
            check("wired-EVApp-drop-pressure", app.contains("installPressureGuard"))

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
            check("wired-LiveVoice-computer-result", liveMic.contains("sendComputerResult("))
            check("wired-LiveVoice-computer-state", liveMic.contains("sendComputerState("))

            let macControl = try read(root.appendingPathComponent("Sources/EV/MacControlService.swift"))
            check("wired-MacControl-inspect", macControl.contains("func inspectUI"))
            check("wired-MacControl-app-action", macControl.contains("func appAction"))
            check("wired-MacControl-music", macControl.contains("func musicPlay"))
            check("wired-MacControl-surface", macControl.contains("func pickUIProcess"))
            check("wired-MacControl-ui-action", macControl.contains("AXUIElementPerformAction"))
            check("wired-MacControl-screen", macControl.contains("CGWindowListCreateImage"))
            check("wired-MacControl-open", macControl.contains("urlForApplication(withBundleIdentifier"))
            check("wired-MacControl-settings-pane", macControl.contains("settingsPanes"))
            let probe = try read(root.appendingPathComponent("Sources/EV/MacControlProbe.swift"))
            check("wired-MacControl-probe", probe.contains("EVIE_COMPUTER_PROBE"))
            check("wired-LiveVoiceMicrophone-removeTapIfNeeded", liveMic.contains("removeTapIfNeeded"))
            check("wired-LiveVoiceMicrophone-AVAudioSafe", liveMic.contains("AVAudioSafe.attachAndPrepare"))
            check("wired-LiveVoiceMicrophone-no-auto-shutdown", liveMic.contains("isAutoShutdownEnabled = false"))
            check("wired-LiveVoiceMicrophone-configure-before-start", liveMic.contains("configure?(engine)"))
            check("wired-LiveVoiceMicrophone-no-input-mixer", !liveMic.contains("keepAlive"))
            check("wired-LiveVoiceMicrophone-no-config-observer", !liveMic.contains("AVAudioEngineConfigurationChange"))
            check("wired-LiveVoiceMicrophone-tap-nodatanow", liveMic.contains(".noDataNow"))
            let api = try read(
                root.deletingLastPathComponent()
                    .appendingPathComponent("ios/EVClient/Sources/EVClient/EVAPIClient.swift")
            )
            check("wired-LiveVoice-hud-decode", liveMic.contains("decodeHUD"))
            check("wired-LiveVoice-dispatch-tool", api.contains("func dispatchTool"))
            check("wired-LiveVoice-issue-reverification", api.contains("func issueReverification"))
            check("wired-LiveVoice-approve-reverify", api.contains("reverifyToken"))
            let coordinator = try read(
                root.deletingLastPathComponent()
                    .appendingPathComponent("ios/EVClient/Sources/EVClient/LiveVoiceCoordinator.swift")
            )
            check("wired-LiveVoiceCoordinator-openLiveVoice", coordinator.contains("openLiveVoice"))
            check("wired-LiveVoiceCoordinator-confirmHold", coordinator.contains("func confirmHold"))
            check("wired-LiveVoiceCoordinator-long-mute", coordinator.contains("timeIntervalSince($0) >= 20"))
            check("wired-LiveVoiceCoordinator-no-transcript-chop", !coordinator.contains("case \"final_transcript\":\n            player.stop()"))
            check("wired-LivePCMPlayer", liveMic.contains("class LivePCMPlayer"))
            check("wired-LivePCMPlayer-no-overrun-reset", !liveMic.contains("maxLeadSeconds"))

            let apiClient = try read(
                root.deletingLastPathComponent()
                    .appendingPathComponent("ios/EVClient/Sources/EVClient/EVAPIClient.swift")
            )
            let apiModels = try read(
                root.deletingLastPathComponent()
                    .appendingPathComponent("ios/EVClient/Sources/EVClient/Models.swift")
            )
            check("wired-EVAPIClient-list-integrations", apiClient.contains("func integrations("))
            check("wired-EVAPIClient-install-integration", apiClient.contains("func installIntegration("))
            check("wired-EVAPIClient-google-oauth", apiClient.contains("beginIntegrationOAuth"))
            check("wired-EVAPIClient-integration-models", apiModels.contains("struct IntegrationRecord") && apiModels.contains("IntegrationOAuthAuthorize"))
        } catch {
            failed += 1
            print("FAIL shipped-wiring-read — \(error)")
        }

        runBargeInDetectorChecks { name, ok, detail in
            check(name, ok, detail)
        }

        runListenerPresenceChecks { name, ok, detail in
            check(name, ok, detail)
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

    /// Source after (and including) the first occurrence of `marker`, if any.
    private static func suffix(_ source: String, from marker: String) -> Substring? {
        source.range(of: marker).map { source[$0.lowerBound...] }
    }

    private static func runBargeInDetectorChecks(_ check: (String, Bool, String) -> Void) {
        let machine = VoiceTurnMachine()
        check("barge-in-starts-listening", machine.currentPhase == .listening, "")
        check("barge-in-accepts-assistant-audio", machine.acceptAssistantChunk(), "")
        check("barge-in-phase-assistant", machine.currentPhase == .assistantSpeaking, "")
        check("barge-in-detector-armed", machine.shouldRunDetector(), "")
        check("barge-in-blocks-provider-during-speech", !machine.canForwardMicToProvider(echoGate: false), "")
        check("barge-in-begin-interrupt", machine.beginInterrupt(), "")
        check("barge-in-latch-once", !machine.beginInterrupt(), "")
        machine.completeInterrupt()
        check("barge-in-user-speaking", machine.currentPhase == .userSpeaking, "")
        check("barge-in-drops-stale-tts", !machine.acceptAssistantChunk(), "")

        let preroll = MicPrerollBuffer(durationMs: 400)
        preroll.append(pcmSine(hz: 180, seconds: 0.4, amp: 0.2))
        let kept = preroll.snapshot(fromOnsetMs: 120, padMs: 60)
        check("barge-in-preroll-keeps-onset", kept.count >= 16_000 * 2 / 1000 * 150, "")
        check("barge-in-preroll-bounded", kept.count <= preroll.capacityBytes, "")

        let echoPlayback = pcmSine(hz: 220, seconds: 0.3, amp: 0.35)
        let echoMic = scaled(echoPlayback, gain: 0.22)
        let echoDetector = BargeInDetector()
        var echoConfirmed = false
        strideFrames(echoMic, playback: echoPlayback, audible: true) { mic, snap in
            if echoDetector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                echoConfirmed = true
            }
        }
        check("barge-in-echo-not-confirmed", !echoConfirmed, "")

        let click = pcmImpulse(samples: 80, amp: 0.9)
        let clickDetector = BargeInDetector()
        let clickSnap = PlaybackSnapshot(audible: true, echoGate: true, playedMs: 400)
        var clickConfirmed = false
        for _ in 0..<8 {
            if clickDetector.analyze(microphonePCM16: click, playback: clickSnap).confirmedUserSpeech {
                clickConfirmed = true
            }
        }
        check("barge-in-click-not-confirmed", !clickConfirmed, "")

        let user = pcmVoiced(seconds: 0.28, amp: 0.16)
        let userDetector = BargeInDetector()
        var userConfirmed = false
        var candidateToConfirm: Double = 0
        strideFrames(user, playback: Data(), audible: false) { mic, snap in
            let decision = userDetector.analyze(microphonePCM16: mic, playback: snap)
            if decision.confirmedUserSpeech {
                userConfirmed = true
                if decision.candidateNs != 0 {
                    candidateToConfirm = Double(
                        decision.confirmedNs.subtractingReportingOverflow(decision.candidateNs).partialValue
                    ) / 1_000_000.0
                }
            }
        }
        check("barge-in-user-speech-confirmed", userConfirmed, "")
        check("barge-in-confirm-under-150ms", candidateToConfirm == 0 || candidateToConfirm <= 150, "ms=\(candidateToConfirm)")

        let overlapPlayback = pcmSine(hz: 240, seconds: 0.28, amp: 0.12)
        let overlapUser = mix(overlapPlayback, pcmVoiced(seconds: 0.28, amp: 0.18))
        let overlapDetector = BargeInDetector()
        var overlapConfirmed = false
        strideFrames(overlapUser, playback: overlapPlayback, audible: true) { mic, snap in
            if overlapDetector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                overlapConfirmed = true
            }
        }
        check("barge-in-overlap-confirmed", overlapConfirmed, "")

        let session = LiveBargeInSession()
        _ = session.machine.acceptAssistantChunk()
        var interrupted = false
        var forwarded = 0
        var prerollBytes = 0
        strideFrames(pcmVoiced(seconds: 0.3, amp: 0.18), playback: Data(), audible: true) { mic, snap in
            session.handleMicFrame(
                mic,
                playback: snap,
                forward: { _ in
                    if !interrupted {
                        forwarded += 1
                    }
                },
                interrupt: { event in
                    interrupted = true
                    prerollBytes = event.preroll.count
                }
            )
        }
        check("barge-in-session-stops-before-forward", interrupted, "")
        check("barge-in-session-no-provider-during-speech", forwarded == 0, "")
        check("barge-in-session-preroll-nonzero", prerollBytes > 0, "")
    }

    // MARK: - Listener presence engine (contextual backchannels)

    /// Deterministic scenario matrix for the EvieListenerPresenceEngine.
    /// Seeded RNG; no audio hardware. Timing law: opportunities come only
    /// from acoustic evidence, never from intervals.
    private static func runListenerPresenceChecks(_ check: (String, Bool, String) -> Void) {
        func variant(_ id: String, _ kind: ListenerFeedbackClass) -> ListenerVariant {
            ListenerVariant(id: id, kind: kind, pcm16: pcmSine(hz: 210, seconds: 0.45, amp: 0.1))
        }
        let library = [
            variant("mhm-1", .neutralContinuer),
            variant("mhm-2", .neutralContinuer),
            variant("mm-1", .neutralContinuer),
            variant("yeah-1", .lightAcknowledgment),
            variant("nice-1", .positiveFeedback),
        ]

        // Detector feed helper: speech at 0.03 RMS, pause near silence.
        @discardableResult
        func feed(
            _ detector: BackchannelOpportunityDetector,
            speechMs: Int,
            pauseMs: Int,
            frameMs: Int = 100,
            startFrame: UInt64 = 1
        ) -> [BackchannelOpportunity] {
            var frame = startFrame
            var out: [BackchannelOpportunity] = []
            let speechFrames = max(0, speechMs / frameMs)
            for _ in 0..<speechFrames {
                if let opp = detector.ingest(OwnerFloorFeatures(frameIndex: frame, rms: 0.03, frameMs: frameMs)) {
                    out.append(opp)
                }
                frame += 1
            }
            let pauseFrames = max(1, pauseMs / frameMs)
            for _ in 0..<pauseFrames {
                if let opp = detector.ingest(OwnerFloorFeatures(frameIndex: frame, rms: 0.0005, frameMs: frameMs)) {
                    out.append(opp)
                }
                frame += 1
            }
            return out
        }

        // S1/S2 — short utterances never produce an opportunity at all.
        do {
            let d = BackchannelOpportunityDetector()
            let opps = feed(d, speechMs: 3_000, pauseMs: 400)
            check("listener-s1-short-turn-no-opportunity", opps.isEmpty, "\(opps.count)")
        }

        // S3 — a long explanation with a clause pause yields exactly one
        // opportunity per pause episode (not per frame, not per interval),
        // with low turn-end probability mid-story.
        do {
            let d = BackchannelOpportunityDetector()
            let opps = feed(d, speechMs: 16_000, pauseMs: 500)
            check("listener-s3-one-per-pause", opps.count == 1, "\(opps.count)")
            if let opp = opps.first {
                check("listener-s3-turn-end-low-midstory", opp.turnEndProbability < 0.62, "\(opp.turnEndProbability)")
            }
        }

        // S6 — turn-end protection: dead air raises p(end); policy never
        // vocalizes there.
        do {
            let d = BackchannelOpportunityDetector()
            let opps = feed(d, speechMs: 12_000, pauseMs: 900)
            let policy = BackchannelPolicy(rng: BackchannelPolicy.SeededRNG(seed: 7))
            var anyVocalAtEnd = false
            for opp in opps where opp.pauseMs >= 800 {
                if case .vocal = policy.decide(
                    opp,
                    context: ListenerTurnContext(),
                    available: library,
                    nowMs: 0
                ) { anyVocalAtEnd = true }
            }
            check("listener-s6-no-vocal-at-turn-end", !anyVocalAtEnd, "opps=\(opps.count)")
        }

        // S4 — perceived randomness: equivalent speech under different seeds
        // produces different valid patterns (no fixed cadence).
        do {
            var signatures: Set<String> = []
            for seed in UInt64(11)...UInt64(18) {
                let d = BackchannelOpportunityDetector()
                let policy = BackchannelPolicy(rng: BackchannelPolicy.SeededRNG(seed: seed))
                var sig = ""
                var nowMs = 0
                var frame: UInt64 = 1
                for _ in 0..<3 {
                    let opps = feed(d, speechMs: 9_000, pauseMs: 420, startFrame: frame)
                    frame += UInt64((9_000 + 420) / 100 + 2)
                    for opp in opps {
                        nowMs += opp.speechMs + opp.pauseMs + 20_000
                        switch policy.decide(
                            opp,
                            context: ListenerTurnContext(),
                            available: library,
                            nowMs: nowMs
                        ) {
                        case .vocal(let kind, let id): sig += "V(\(kind.rawValue):\(id))"
                        case .visualOnly: sig += "n"
                        case .nothing: sig += "."
                        }
                    }
                }
                signatures.insert(sig)
            }
            check("listener-s4-patterns-vary-by-seed", signatures.count >= 3, "unique=\(signatures.count)")
        }

        // Anti-repetition: consecutive vocal picks never repeat a variant.
        // Budget ceiling raised so this check exercises repetition only.
        do {
            let policy = BackchannelPolicy(
                config: .init(emitScale: 1.0, refractoryFloorMs: 0, refractoryJitterMs: 1_000, firstCueAfterMs: 0, maxVocalPerTurn: 50),
                rng: BackchannelPolicy.SeededRNG(seed: 42)
            )
            var lastId: String?
            var repeats = 0
            var vocals = 0
            var nowMs = 20_000
            for i in 0..<40 {
                let opp = BackchannelOpportunity(
                    atFrame: UInt64(i * 3 + 2), speechMs: 10_000, pauseMs: 300,
                    entryDecayRatio: 0.2, turnEndProbability: 0.1, adaptedPauseMs: 700,
                    partialActive: false
                )
                if case .vocal(_, let id) = policy.decide(
                    opp, context: ListenerTurnContext(), available: library, nowMs: nowMs
                ) {
                    vocals += 1
                    if id == lastId { repeats += 1 }
                    lastId = id
                }
                nowMs += 30_000
            }
            check("listener-anti-repetition", vocals >= 10 && repeats == 0, "vocals=\(vocals) repeats=\(repeats)")
        }

        // Budget: hard ceiling of vocal cues per owner turn.
        do {
            let policy = BackchannelPolicy(config: .init(emitScale: 1.0), rng: BackchannelPolicy.SeededRNG(seed: 99))
            var nowMs = 20_000
            var vocals = 0
            for i in 0..<30 {
                let opp = BackchannelOpportunity(
                    atFrame: UInt64(i * 7 + 3), speechMs: 25_000, pauseMs: 350,
                    entryDecayRatio: 0.15, turnEndProbability: 0.05, adaptedPauseMs: 700,
                    partialActive: false
                )
                if case .vocal = policy.decide(
                    opp, context: ListenerTurnContext(), available: library, nowMs: nowMs
                ) { vocals += 1 }
                nowMs += 40_000
            }
            check("listener-budget-cap", vocals <= 3, "vocals=\(vocals)")
        }

        // S13/S15 — sensitive/authorization contexts may only produce
        // silence or NEUTRAL continuers, across many seeds.
        do {
            var badKind: String?
            for s in 0..<60 {
                let policy = BackchannelPolicy(config: .init(emitScale: 1.0), rng: BackchannelPolicy.SeededRNG(seed: UInt64(s)))
                let opp = BackchannelOpportunity(
                    atFrame: UInt64(s + 100), speechMs: 12_000, pauseMs: 320,
                    entryDecayRatio: 0.2, turnEndProbability: 0.1, adaptedPauseMs: 700,
                    partialActive: true
                )
                let ctx = ListenerTurnContext(partialTail: "so maybe we should delete production data")
                if case .vocal(let kind, _) = policy.decide(
                    opp, context: ctx, available: library, nowMs: 60_000
                ), kind != .neutralContinuer {
                    badKind = kind.rawValue
                }
            }
            check("listener-s15-risk-neutral-only", badKind == nil, badKind ?? "")
        }

        // S14 — positive-result context can unlock positiveFeedback.
        do {
            var sawPositive = false
            for s in 0..<40 {
                let policy = BackchannelPolicy(config: .init(emitScale: 1.0), rng: BackchannelPolicy.SeededRNG(seed: UInt64(s)))
                let opp = BackchannelOpportunity(
                    atFrame: UInt64(s + 50), speechMs: 11_000, pauseMs: 300,
                    entryDecayRatio: 0.2, turnEndProbability: 0.05, adaptedPauseMs: 700,
                    partialActive: true
                )
                let ctx = ListenerTurnContext(
                    partialTail: "and then everything finally worked and passed",
                    semanticGesturesAllowed: true
                )
                if case .vocal(let kind, _) = policy.decide(
                    opp, context: ctx, available: library, nowMs: 60_000
                ), kind == .positiveFeedback { sawPositive = true }
            }
            check("listener-s14-positive-unlocked", sawPositive, "")
        }

        // SEMANTIC GESTURES DISABLED (recovery law): even with strong lexical
        // evidence, semanticGesturesAllowed=false pins selection to neutral
        // continuers across many seeds.
        do {
            var badKind: String?
            for s in 0..<40 {
                let policy = BackchannelPolicy(config: .init(emitScale: 1.0), rng: BackchannelPolicy.SeededRNG(seed: UInt64(s)))
                let opp = BackchannelOpportunity(
                    atFrame: UInt64(s + 70), speechMs: 11_000, pauseMs: 300,
                    entryDecayRatio: 0.2, turnEndProbability: 0.05, adaptedPauseMs: 700,
                    partialActive: true
                )
                let ctx = ListenerTurnContext(
                    partialTail: "and then everything finally worked and passed",
                    semanticGesturesAllowed: false
                )
                if case .vocal(let kind, _) = policy.decide(
                    opp, context: ctx, available: library, nowMs: 60_000
                ), kind != .neutralContinuer { badKind = kind.rawValue }
            }
            check("listener-semantic-disabled-neutral-only", badKind == nil, badKind ?? "")
        }

        // Priority — pending assistant response suppresses everything.
        do {
            let policy = BackchannelPolicy(rng: BackchannelPolicy.SeededRNG(seed: 3))
            let opp = BackchannelOpportunity(
                atFrame: 10, speechMs: 15_000, pauseMs: 300,
                entryDecayRatio: 0.2, turnEndProbability: 0.1, adaptedPauseMs: 700,
                partialActive: false
            )
            let decision = policy.decide(
                opp,
                context: ListenerTurnContext(assistantResponsePending: true),
                available: library,
                nowMs: 50_000
            )
            check("listener-priority-response-first", decision == .nothing(.assistantResponsePending), "\(decision)")
        }

        // Session-local adaptation clamps toward this owner's pauses.
        do {
            let pauses = Array(repeating: 380, count: 12)
            let cap = BackchannelOpportunityDetector.adaptedMaxPause(pauses, fallback: 700)
            check("listener-adaptation-clamps-toward-owner", cap >= 340 && cap <= 420, "cap=\(cap)")
            check("listener-adaptation-fallback-without-data", BackchannelOpportunityDetector.adaptedMaxPause([], fallback: 700) == 700, "")
        }

        // ---- Round Two overlap semantics (directive: LP canary round one) ----

        // A nod-shaped auxiliary signal (soft 190 Hz hum, like the shipped
        // clips) used as SELF playback in the detector scenarios below.
        let nod = pcmSine(hz: 190, seconds: 0.7, amp: 0.12)

        // OVERLAP REGRESSION (A1/A2/A8): owner keeps speaking through an
        // entire nod. The detector must still confirm the OWNER (capture and
        // forwarding continue), and — structurally, via the aux lane — that
        // confirmation cannot cancel the nod.
        do {
            let owner = pcmVoiced(seconds: Double(nod.count / 2) / 16_000, amp: 0.18)
            let mixed = mix(nod, owner)
            let detector = BargeInDetector()
            var confirmed = false
            strideFrames(mixed, playback: nod, audible: true) { mic, snap in
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("listener-overlap-owner-still-confirmed", confirmed, "owner speech must survive a simultaneous nod")
        }

        // SELF-ECHO: the nod alone, leaking back through the mic at room
        // gain, must never read as user speech (it would chop ROLE C speech
        // for nothing and pollute the turn machine).
        do {
            let leak = scaled(nod, gain: 0.25)
            let detector = BargeInDetector()
            var confirmed = false
            strideFrames(leak, playback: nod, audible: true) { mic, snap in
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("listener-self-nod-not-confirmed", !confirmed, "Evie's own nod echo must not confirm as barge-in")
        }

        // REFERENCE-UNRELIABLE GUARD (P0 round three): speakers audibly live
        // (audible=true) but the playback reference reads silent — the exact
        // hollow-reference state that chopped Evie's replies every ~2 s.
        // Clause-pause-level echo energy must NOT confirm; real near-end
        // speech must still confirm.
        do {
            // Evie speaking: her voice leaks to the mic at 0.02 RMS while the
            // reference is empty (audible=true, pcm empty → play_rms≈0).
            let echoLeak = pcmVoiced(seconds: 0.6, amp: 0.02)
            let detector = BargeInDetector()
            var confirmed = false
            strideFrames(echoLeak, playback: Data(), audible: true) { mic, snap in
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("listener-hollow-reference-echo-not-confirmed", !confirmed, "self speech under hollow reference must never chop the response")
        }
        do {
            // The owner genuinely says "Wait." at real near-end level while
            // the same inconsistent state holds — barge-in must still work.
            let owner = pcmVoiced(seconds: 0.6, amp: 0.16)
            let detector = BargeInDetector()
            var confirmed = false
            strideFrames(owner, playback: Data(), audible: true) { mic, snap in
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("listener-hollow-reference-owner-still-confirmed", confirmed, "real near-end speech must survive the guard")
        }

        // DORMANCY PREDICATE: hard floor gate is pure and total.
        do {
            func dormant(_ pending: Bool, _ playing: Bool, _ tail: Bool) -> Bool {
                ListenerFloorGate.isDormant(
                    assistantResponsePending: pending,
                    responseLanePlaying: playing,
                    echoTailActive: tail
                )
            }
            check(
                "listener-dormancy-assistant-floor",
                dormant(true, false, false) && dormant(false, true, false) && dormant(false, false, true),
                "any assistant-floor signal must fully suppress opportunity generation"
            )
            check("listener-dormancy-owner-floor-clear", !dormant(false, false, false), "owner floor with clean tail must be active")
        }

        // ---- ROUND FOUR: false self-turn signatures (proven 23:37–23:38) ----

        // SIGNATURE 1 — provider streaming gap drained the queue (audible
        // false, episode still active). Measured room/tail transients sit at
        // ≤0.0138 RMS; they must never become a user turn.
        do {
            let blip = pcmVoiced(seconds: 0.5, amp: 0.02)
            let detector = BargeInDetector()
            var confirmed = false
            strideFrames(blip, playback: Data(), audible: false) { mic, snap in
                var snap = snap
                snap.assistantEpisodeActive = true
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("turn-episode-drain-blip-not-confirmed", !confirmed, "self/room audio during an assistant episode must never become a user turn")
        }
        do {
            // Same drained state; the OWNER speaks normally ("Wait" measured
            // 0.02–0.04 RMS this session). Must confirm WITHOUT shouting.
            // This validates BARGE-IN V2 (evidence-fusion); run explicitly
            // with V2 enabled because the baseline OFF intentionally rejects
            // normal-volume "Wait" to preserve continuity (owner-verified).
            var cfg = BargeInDetector.Config()
            cfg.v2EpisodeGate = true
            let owner = pcmVoiced(seconds: 0.5, amp: 0.056)
            let detector = BargeInDetector(config: cfg)
            var confirmed = false
            strideFrames(owner, playback: Data(), audible: false) { mic, snap in
                var snap = snap
                snap.assistantEpisodeActive = true
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("turn-episode-drain-soft-wait-confirmed", confirmed, "normal-volume Wait must survive provider-gap drains [V2]")
        }

        // SIGNATURE 2 — clause-pause room noise under a warm ring: the room
        // calibrates the echo EMA from ambient leak (~0.006 RMS at mic), so
        // the candidate floor rises above the measured room-noise ceiling
        // and a sustained ~0.0124 blip cannot become an interruption.
        do {
            let ambient = pcmSine(hz: 220, seconds: 0.5, amp: 0.05)
            let detector = BargeInDetector()
            var confirmed = false
            // Calibrate: ambient leak frames during the episode.
            strideFrames(ambient, playback: ambient, audible: true) { mic, snap in
                var snap = snap
                snap.assistantEpisodeActive = true
                _ = detector.analyze(microphonePCM16: scaled(mic, gain: 0.17), playback: snap)
            }
            let blip = pcmVoiced(seconds: 0.5, amp: 0.012)
            strideFrames(blip, playback: ambient, audible: true) { mic, snap in
                var snap = snap
                snap.assistantEpisodeActive = true
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("turn-episode-warm-ring-blip-not-confirmed", !confirmed, "clause-pause room noise calibrated to its own room must not confirm")
        }

        // CLASS C — SOFT OWNER OVER EVIE (the actual barge-in problem):
        // Evie plays hot (broadband multi-tone, no slow beat vs owner);
        // owner says a soft "Wait" UNDER her volume. The matched filter must
        // strip the echo component and confirm what remains — the owner does
        // NOT need to overpower the speaker.
        // Validates BARGE-IN V2; baseline OFF intentionally rejects this.
        do {
            let evie = pcmSine(hz: 250, seconds: 0.6, amp: 0.055)
            let evie2 = pcmSine(hz: 317, seconds: 0.6, amp: 0.045)
            let evie3 = pcmSine(hz: 433, seconds: 0.6, amp: 0.04)
            let evieMix = mix(mix(evie, evie2), evie3)
            let echoLeak = scaled(evieMix, gain: 0.18)
            let softWait = pcmVoiced(seconds: 0.6, amp: 0.05)
            let mixed = mix(echoLeak, softWait)
            var cfg = BargeInDetector.Config()
            cfg.v2EpisodeGate = true
            let detector = BargeInDetector(config: cfg)
            var confirmed = false
            var confirmFrame = 0
            var idx = 0
            strideFrames(mixed, playback: evieMix, audible: true) { mic, snap in
                var snap = snap
                snap.assistantEpisodeActive = true
                idx += 1
                if !confirmed,
                   detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                    confirmFrame = idx
                }
            }
            check("turn-classc-soft-wait-over-evie-confirmed", confirmed, "soft owner speech over playback must confirm (matched-filter ownership) [V2]")
            check("turn-classc-confirm-within-120ms", confirmFrame > 0 && confirmFrame <= 6, "confirmation within ~120 ms of onset (frames=\(confirmFrame)) [V2]")
        }

        // ADAPTIVE GATE — loud playback raises the bar above direct echo:
        // reference 0.35 RMS, mic = 22% leak → rejected.
        do {
            let loud = pcmSine(hz: 220, seconds: 0.5, amp: 0.35)
            let leak = scaled(loud, gain: 0.22)
            let detector = BargeInDetector()
            var confirmed = false
            strideFrames(leak, playback: loud, audible: true) { mic, snap in
                var snap = snap
                snap.assistantEpisodeActive = true
                if detector.analyze(microphonePCM16: mic, playback: snap).confirmedUserSpeech {
                    confirmed = true
                }
            }
            check("turn-episode-loud-echo-not-confirmed", !confirmed, "loud-playback echo stays self audio")
        }

        // QUARANTINE RELEASE — recovering exits only after sustained quiet,
        // and forwarding stays blocked until it does.
        do {
            let session = LiveBargeInSession()
            session.machine.acceptAssistantChunk()
            session.machine.notePlaybackEnded()
            check("turn-quarantine-starts-recovering", session.machine.currentPhase == .recovering, "")
            let frameMs60 = Data(count: 16000 * 2 * 60 / 1000)
            // Voiced frames must NOT open the floor…
            let voiced = pcmVoiced(seconds: 0.06, amp: 0.02)
            for _ in 0..<20 {
                session.handleMicFrame(
                    voiced,
                    playback: .silent,
                    forward: { _ in },
                    interrupt: { _ in }
                )
            }
            check("turn-quarantine-holds-under-voiced-blips", session.machine.currentPhase == .recovering, "voiced self-tail resets the decay clock")
            // …sustained quiet does.
            for _ in 0..<10 {
                session.handleMicFrame(
                    frameMs60,
                    playback: .silent,
                    forward: { _ in },
                    interrupt: { _ in }
                )
            }
            check("turn-quarantine-releases-on-decay", session.machine.currentPhase == .listening, "decayed room tail reopens owner floor")
        }

        // DURATION FAMILIES ARE CONTEXTUAL (policy): elongated only at a
        // long-held floor + low turn-end probability + sparse recent vocal;
        // otherwise normal/subtle shapes are preferred.
        do {
            let libraryFam: [ListenerVariant] = [
                ListenerVariant(id: "n-a", kind: .neutralContinuer, pcm16: Data([0, 0]), family: .normal),
                ListenerVariant(id: "e-a", kind: .neutralContinuer, pcm16: Data([0, 0]), family: .elongated),
            ]
            func pick(_ speechMs: Int, _ turnEnd: Float, vocalThisTurn: Int) -> String? {
                let policy = BackchannelPolicy(
                    config: .init(emitScale: 1.0, refractoryFloorMs: 0, refractoryJitterMs: 0, firstCueAfterMs: 0),
                    rng: BackchannelPolicy.SeededRNG(seed: 5)
                )
                guard case .vocal(_, let id) = policy.decide(
                    BackchannelOpportunity(
                        atFrame: 9, speechMs: speechMs, pauseMs: 320,
                        entryDecayRatio: 0.2, turnEndProbability: turnEnd,
                        adaptedPauseMs: 700, partialActive: false
                    ),
                    context: ListenerTurnContext(),
                    available: libraryFam,
                    nowMs: 30_000
                ) else { return nil }
                return id
            }
            var elongatedAtStrongPoint = false
            var noneElongatedEarlyOrNearEnd = true
            for seed in UInt64(0)...20 {
                let policyA = BackchannelPolicy(
                    config: .init(emitScale: 1.0, refractoryFloorMs: 0, refractoryJitterMs: 0, firstCueAfterMs: 0),
                    rng: BackchannelPolicy.SeededRNG(seed: 100 + seed)
                )
                if case .vocal(_, let id) = policyA.decide(
                    BackchannelOpportunity(
                        atFrame: 11, speechMs: 14_000, pauseMs: 320,
                        entryDecayRatio: 0.2, turnEndProbability: 0.10,
                        adaptedPauseMs: 700, partialActive: false
                    ),
                    context: ListenerTurnContext(),
                    available: libraryFam,
                    nowMs: 40_000
                ), id == "e-a" { elongatedAtStrongPoint = true }
                let idEarly = pick(6_000, 0.10, vocalThisTurn: 0)
                let idNearEnd = pick(14_000, 0.55, vocalThisTurn: 0)
                if idEarly == "e-a" || idNearEnd == "e-a" { noneElongatedEarlyOrNearEnd = false }
            }
            check("listener-elongated-at-strong-continuation", elongatedAtStrongPoint, "long floor + low turn-end should reach the elongated pool")
            check("listener-no-elongated-early-or-near-end", noneElongatedEarlyOrNearEnd, "young turns and near-turn-end stay short/silent")
        }
    }

    private static func pcmSine(hz: Double, seconds: Double, amp: Float, sampleRate: Int = 16_000) -> Data {
        let n = max(1, Int(Double(sampleRate) * seconds))
        var data = Data(count: n * 2)
        data.withUnsafeMutableBytes { raw in
            let dst = raw.bindMemory(to: Int16.self)
            for i in 0..<n {
                let sample = sin(2.0 * Double.pi * hz * Double(i) / Double(sampleRate))
                let value = max(-1, min(1, Float(sample) * amp))
                dst[i] = Int16((value * 32767.0).rounded())
            }
        }
        return data
    }

    private static func pcmVoiced(seconds: Double, amp: Float) -> Data {
        let a = pcmSine(hz: 180, seconds: seconds, amp: amp)
        let b = pcmSine(hz: 360, seconds: seconds, amp: amp * 0.45)
        return mix(a, b)
    }

    private static func pcmImpulse(samples: Int, amp: Float) -> Data {
        var data = Data(count: max(2, samples * 2))
        data.withUnsafeMutableBytes { raw in
            let dst = raw.bindMemory(to: Int16.self)
            for i in 0..<samples {
                dst[i] = 0
            }
            dst[0] = Int16((amp * 32767.0).rounded())
        }
        return data
    }

    private static func scaled(_ pcm: Data, gain: Float) -> Data {
        var out = pcm
        let n = out.count / 2
        out.withUnsafeMutableBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<n {
                let value = Float(samples[i]) * gain
                samples[i] = Int16(max(-32767, min(32767, value.rounded())))
            }
        }
        return out
    }

    private static func mix(_ a: Data, _ b: Data) -> Data {
        let n = min(a.count, b.count) / 2
        var out = Data(count: n * 2)
        a.withUnsafeBytes { rawA in
            b.withUnsafeBytes { rawB in
                out.withUnsafeMutableBytes { rawO in
                    let sa = rawA.bindMemory(to: Int16.self)
                    let sb = rawB.bindMemory(to: Int16.self)
                    let so = rawO.bindMemory(to: Int16.self)
                    for i in 0..<n {
                        let sum = Int(sa[i]) + Int(sb[i])
                        so[i] = Int16(max(-32767, min(32767, sum)))
                    }
                }
            }
        }
        return out
    }

    private static func strideFrames(
        _ pcm: Data,
        playback: Data,
        audible: Bool,
        handle: (Data, PlaybackSnapshot) -> Void
    ) {
        let frame = 320 * 2
        var offset = 0
        while offset + frame <= pcm.count {
            let mic = pcm.subdata(in: offset..<(offset + frame))
            let play: Data
            if playback.count >= offset + frame {
                play = playback.subdata(in: offset..<(offset + frame))
            } else {
                play = Data()
            }
            let rms = BargeInDetector.rms(BargeInDetector.floatSamples(play))
            handle(
                mic,
                PlaybackSnapshot(
                    pcm16: play,
                    rms: rms,
                    audible: audible && !play.isEmpty,
                    echoGate: audible,
                    playedMs: offset * 1000 / (16_000 * 2),
                    queuedMs: 200
                )
            )
            offset += frame
        }
    }
}
