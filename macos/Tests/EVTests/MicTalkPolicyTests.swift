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
            check("wired-LiveConversation-echo-gate", live.contains("shouldMuteCapture"))
            check("wired-LiveConversation-drain-watchdog", live.contains("noteAssistantAudioComplete()"))
            check("wired-LiveConversation-no-transcript-chop", !live.contains("prepareForNewTurn"))
            check("wired-LiveConversation-mic-recover", live.contains("microphone.recover()"))

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
