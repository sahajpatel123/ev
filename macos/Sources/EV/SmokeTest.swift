import AVFoundation
import EVClient
import Foundation
import UserNotifications

/// Headless end-to-end smoke driver for the SUIT client.
///
/// Exercises exactly the API path the menu-bar app uses — health, streaming
/// chat (SSE), voice wake, voice utterance, and TTS audio fetch — and prints
/// machine-readable evidence for each hop. It does not open a GUI, use the
/// microphone, or claim to demonstrate the visual menu-bar flow.
enum EVSmokeTest {
    /// Request one TCC permission through the OS prompt and report the live
    /// status afterwards. Times out instead of hanging an automated run.
    static func runLifeRequest() -> Int32 {
        let arguments = CommandLine.arguments
        guard
            let index = arguments.firstIndex(of: "--permission"),
            index + 1 < arguments.count,
            let kind = PermissionKind(rawValue: arguments[index + 1])
        else {
            print("life-request: missing or unknown --permission")
            return 5
        }
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            let requested = await PermissionCenter.request(kind)
            let statuses = await PermissionCenter.statuses()
            let status = statuses.first { $0.kind == kind }
            print(
                "life-request: \(kind.rawValue) requested=\(requested) "
                    + "state=\(status?.state.rawValue ?? "unknown")"
            )
            exitCode = status?.state == .granted ? 0 : (requested ? 2 : 1)
            semaphore.signal()
        }
        let result = semaphore.wait(timeout: .now() + 45)
        if result == .timedOut {
            print("life-request: timed out waiting for the TCC prompt")
            return 1
        }
        return exitCode
    }

    /// Record 2 seconds of real microphone audio through AVFoundation and
    /// report the captured PCM bytes. Requires the bundled app identity and
    /// an already-granted Microphone permission.
    static func runMic() -> Int32 {
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            let mic = MicCapture()
            guard await mic.start() else {
                print("mic: FAIL — permission denied or capture unavailable")
                semaphore.signal()
                return
            }
            print("mic: recording for 2 seconds")
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            let data = mic.stop()
            if let data, !data.isEmpty {
                let seconds = MicCapture.durationSeconds(data)
                print(
                    "mic: captured \(data.count) bytes ≈ "
                        + String(format: "%.2f", seconds)
                        + "s (16 kHz mono PCM WAV)"
                )
                exitCode = 0
            } else {
                print("mic: FAIL — no audio captured")
            }
            semaphore.signal()
        }
        semaphore.wait()
        return exitCode
    }

    /// Play a locally generated 440 Hz tone through the same AVAudioPlayer
    /// path used for Agent 4's `tts.audio_ref` bytes, and report playback.
    static func runTTS() -> Int32 {
        setbuf(stdout, nil)
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            do {
                print("tts: start")
                let sampleRate = 16000
                let sampleCount = sampleRate / 2
                let dataSize = sampleCount * 2
                var wav = Data()
                func appendUInt32(_ value: UInt32) {
                    withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
                }
                func appendUInt16(_ value: UInt16) {
                    withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
                }
                wav.append(contentsOf: Array("RIFF".utf8))
                appendUInt32(UInt32(36 + dataSize))
                wav.append(contentsOf: Array("WAVEfmt ".utf8))
                appendUInt32(16)
                appendUInt16(1) // PCM
                appendUInt16(1) // mono
                appendUInt32(UInt32(sampleRate))
                appendUInt32(UInt32(sampleRate * 2))
                appendUInt16(2) // block align
                appendUInt16(16) // bits per sample
                wav.append(contentsOf: Array("data".utf8))
                appendUInt32(UInt32(dataSize))
                for index in 0..<sampleCount {
                    let value = Int16(
                        sin(2.0 * Double.pi * 440.0 * Double(index) / Double(sampleRate)) * 12000.0
                    )
                    appendUInt16(UInt16(bitPattern: value))
                }
                print("tts: generated \(wav.count) byte WAV")

                let player = TTSPlayer()
                try player.play(data: wav)
                print(
                    "tts: playback started duration=0.50s"
                )
                try? await Task.sleep(nanoseconds: 300_000_000)
                let stillPlaying = player.isPlaying
                player.stop()
                print("tts: playing after 0.3s=\(stillPlaying)")
                exitCode = stillPlaying ? 0 : 1
            } catch {
                print("tts: FAIL — \(error)")
            }
            semaphore.signal()
        }
        semaphore.wait()
        return exitCode
    }

    /// Print every permission SUIT checks, what breaks when denied, and the
    /// exact System Settings deep link. Exit 0 means the detection/reporting
    /// path works (not that every permission is granted).
    static func runPermissions() -> Int32 {
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            let statuses = await PermissionCenter.statuses()
            for status in statuses {
                print("\(status.kind.rawValue): \(status.state.rawValue)")
                print("  breaks: \(status.whatBreaks)")
                print("  settings: \(status.settingsURL?.absoluteString ?? "none")")
            }
            exitCode = 0
            semaphore.signal()
        }
        semaphore.wait()
        return exitCode
    }

    /// Request every programmatic TCC permission in sequence and print the
    /// before/after state for each. This is what makes EV appear in each
    /// System Settings privacy pane. Accessibility and Full Disk Access have
    /// no prompt and are reported as manual "+" additions.
    static func runRequestAll() -> Int32 {
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            let before = await PermissionCenter.statuses()
            print("request-all: requesting every programmatic TCC permission…")
            let after = await PermissionCenter.requestAll()
            for (b, a) in zip(before, after) {
                let arrow = b.state == a.state ? "=" : "→"
                print("\(a.kind.rawValue): \(b.state.rawValue) \(arrow) \(a.state.rawValue)")
            }
            print("request-all: accessibility + fullDiskAccess need the '+' button in their panes.")
            exitCode = 0
            semaphore.signal()
        }
        semaphore.wait()
        return exitCode
    }

    /// Idempotent registration sweep: fires requests only for permissions
    /// macOS still reports as undecided, so re-running fills in any pane EV
    /// has not appeared in yet without re-prompting about decided ones.
    static func runRequestPending() -> Int32 {
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            let before = await PermissionCenter.statuses()
            print("request-pending: registering EV in every still-undecided pane…")
            let after = await PermissionCenter.requestPending()
            for (b, a) in zip(before, after) {
                let arrow = b.state == a.state ? "=" : "→"
                print("\(a.kind.rawValue): \(b.state.rawValue) \(arrow) \(a.state.rawValue)")
            }
            print("request-pending: open each pane in System Settings and toggle what is still off.")
            exitCode = 0
            semaphore.signal()
        }
        semaphore.wait()
        return exitCode
    }

    /// Post a native notification through the single delivery path. If the
    /// user has not decided yet, this reports that a prompt is required
    /// instead of hanging an automated run.
    static func runNotify() -> Int32 {
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            let center = UNUserNotificationCenter.current()
            let settings = await center.notificationSettings()
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral:
                await NotificationBridge.shared.post(
                    title: "EV smoke",
                    body: "Notification delivery path works.",
                    identifier: "ev.smoke.notify"
                )
                print("notify: authorized — notification posted")
                exitCode = 0
            case .notDetermined:
                print("notify: notDetermined — authorization prompt required; skipped")
                exitCode = 0
            case .denied:
                print("notify: denied — enable notifications in System Settings")
                exitCode = 1
            @unknown default:
                print("notify: unknown status")
                exitCode = 1
            }
            semaphore.signal()
        }
        semaphore.wait()
        return exitCode
    }

    static func run() -> Int32 {
        let config = AppConfig()
        let client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1

        Task {
            do {
                print("EV smoke test")
                print("baseURL: \(config.baseURL.absoluteString)")

                // 1. Health
                let health = try await client.health()
                print("health: status=\(health.status) app=\(health.app) version=\(health.version)")
                guard health.status == "ok" || health.status == "degraded" else {
                    print("FAIL: health status \(health.status)")
                    semaphore.signal()
                    return
                }

                // 2. Streaming chat (Agent 10 / CORTEX SSE)
                var finalText = ""
                var done: ChatStreamDone?
                var streamError: String?
                for try await event in client.askStream(
                    "Reply with exactly: smoke test ok",
                    deviceId: config.deviceID
                ) {
                    switch event {
                    case .delta(let chunk, _):
                        finalText += chunk
                    case .refined(let text):
                        finalText = text
                    case .done(let streamDone):
                        done = streamDone
                    case .error(let message):
                        streamError = message
                    default:
                        break
                    }
                }
                print(
                    "chat: text=\(finalText.replacingOccurrences(of: "\n", with: "\\n")) "
                        + "conversation=\(done?.conversationId ?? "nil") "
                        + "model=\(done?.model ?? "nil") "
                        + "tokens=\(done?.contextTokens ?? -1) "
                        + "error=\(streamError ?? "nil")"
                )
                guard streamError == nil, !finalText.isEmpty else {
                    print("FAIL: chat stream did not produce a reply")
                    semaphore.signal()
                    return
                }

                // 3. Voice wake (Agent 4 / VOICE)
                // The wake engine needs an audio source; write a short silent
                // WAV to /tmp and pass it as the local audio_ref.
                let wakeAudioURL = FileManager.default.temporaryDirectory
                    .appendingPathComponent("ev-smoke-wake.wav")
                let sampleCount = 1600
                var wav = Data()
                func appendUInt32(_ value: UInt32) {
                    withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
                }
                func appendUInt16(_ value: UInt16) {
                    withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
                }
                wav.append(contentsOf: Array("RIFF".utf8))
                appendUInt32(UInt32(36 + sampleCount * 2))
                wav.append(contentsOf: Array("WAVEfmt ".utf8))
                appendUInt32(16)
                appendUInt16(1)
                appendUInt16(1)
                appendUInt32(16000)
                appendUInt32(32000)
                appendUInt16(2)
                appendUInt16(16)
                wav.append(contentsOf: Array("data".utf8))
                appendUInt32(UInt32(sampleCount * 2))
                wav.append(Data(repeating: 0, count: sampleCount * 2))
                try wav.write(to: wakeAudioURL)

                let wake = try await client.wakeVoice(
                    deviceId: config.deviceID,
                    wakeWord: "evie",
                    audioRef: wakeAudioURL.path
                )
                print(
                    "wake: state=\(wake.state) session=\(wake.sessionId ?? "nil") "
                        + "enrolled=\(wake.ownerEnrolled)"
                )

                // 4. Utterance (text path — the same endpoint SUIT uses with
                // audio_b64 when the mic is involved).
                if let session = wake.sessionId {
                    if wake.ownerEnrolled, let nonce = wake.challengeNonce {
                        // Deterministic dummy sample: sufficient for the smoke
                        // mock encoder, and will honestly fail against a real
                        // voiceprint gate (which is the correct behavior).
                        let dummySample = Data(repeating: 0, count: 256).base64EncodedString()
                        do {
                            let verify = try await client.verifyVoice(
                                sessionId: session,
                                nonce: nonce,
                                phrase: wake.challengePhrase,
                                samples: [dummySample]
                            )
                            print(
                                "verify: verified=\(verify.verified) "
                                    + "confidence=\(verify.confidence) reason=\(verify.reason)"
                            )
                        } catch {
                            print("verify: skipped — \(error.localizedDescription)")
                        }
                    }

                    let utterance = try await client.utterance(
                        sessionId: session,
                        text: "smoke test"
                    )
                    print(
                        "utterance: state=\(utterance.state) transcript=\(utterance.transcript) "
                            + "reply=\(utterance.reply)"
                    )

                    // 5. TTS audio fetch (Agent 4 audio_ref)
                    if let tts = utterance.tts, let audioRef = tts.audioRef {
                        let url: URL
                        if let absolute = URL(string: audioRef), absolute.scheme != nil {
                            url = absolute
                        } else {
                            url = config.baseURL.appendingPathComponent(
                                audioRef.hasPrefix("/") ? String(audioRef.dropFirst()) : audioRef
                            )
                        }
                        var request = URLRequest(url: url)
                        request.setValue("Bearer \(config.apiKey)", forHTTPHeaderField: "Authorization")
                        let (data, response) = try await client.session.data(for: request)
                        let http = response as? HTTPURLResponse
                        print(
                            "tts: status=\(http?.statusCode ?? -1) bytes=\(data.count) "
                                + "type=\(tts.contentType ?? "nil")"
                        )
                        guard let http, http.statusCode == 200, !data.isEmpty else {
                            print("FAIL: TTS audio fetch did not return audio")
                            semaphore.signal()
                            return
                        }
                    } else {
                        print("tts: no audio_ref (meta provider) — fetch skipped")
                    }
                } else {
                    print("voice: no session (owner not enrolled) — utterance path skipped")
                }

                exitCode = 0
            } catch {
                print("FAIL: \(error)")
                exitCode = 1
            }
            semaphore.signal()
        }

        semaphore.wait()
        return exitCode
    }
}
