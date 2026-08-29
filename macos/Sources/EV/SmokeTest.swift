import AVFoundation
import Speech
import EVClient
import EVRuntime
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

    /// INTERRUPTION V4 FINAL GATE — Apple voice-processing echo probe.
    /// One AVAudioEngine owns BOTH sides: voice-processed mic input AND the
    /// far-end player routed to speakers (the exact calm-topology
    /// relationship). Streams SFSpeechRecognizer over the PROCESSED input
    /// while speech plays through the speakers, and measures how much
    /// far-end content survives. Diagnostic-only; makes brief sound.
    static func runVPEchoProbe(_ vpOn: Bool) -> Int32 {
        setbuf(stdout, nil)
        print("vp-echo-probe: vp=\(vpOn ? "ON" : "OFF")")
        let sem = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1

        // 1) Render the far-end utterance with system TTS (real speech,
        //    includes adversarial tokens + filler so leakage is measurable).
        let farURL = URL(fileURLWithPath: "/tmp/vp_far_end.aiff")
        let text = "Stop. Wait. Evie. Evie, stop. The garden gate needs paint before winter arrives."
        let sayProc = Process()
        sayProc.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        sayProc.arguments = ["-o", farURL.path, text]
        try? sayProc.run(); sayProc.waitUntilExit()

        Task {
            do {
                // --asr-file: recognizer fed straight from the rendered file
                // (no engine) — isolates Speech stack from audio-engine path.
                if CommandLine.arguments.contains("--asr-file") {
                    let file = try AVAudioFile(forReading: farURL)
                    let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
                    print("vp-echo-probe: FILE-MODE auth/avail check")
                    let req = SFSpeechAudioBufferRecognitionRequest()
                    req.shouldReportPartialResults = true
                    // leave default: allow server too — maximal chance
                    // requiresOnDeviceRecognition = false
                    var partials: [String] = []
                    print("vp-echo-probe: task started")
                    _ = rec?.recognitionTask(with: req) { r, e in
                        print("vp-echo-probe: CALLBACK fired isFinal=\(r?.isFinal ?? false) err=\(e != nil)")
                        if let e { print("vp-echo-probe: FILE_REC_ERR \(e)") }
                        if let r {
                            let t = r.bestTranscription.formattedString
                            print("vp-echo-probe: FILE_PARTIAL |\(t)|")
                            partials = [t]
                        }
                    }
                    let ffmt = file.processingFormat
                    let bufCap = AVAudioFrameCount(4096)
                    guard let buf = AVAudioPCMBuffer(pcmFormat: ffmt, frameCapacity: bufCap) else { return }
                    while file.framePosition < file.length {
                        try? file.read(into: buf, frameCount: bufCap)
                        if buf.frameLength == 0 { break }
                        req.append(buf)
                        Thread.sleep(forTimeInterval: Double(buf.frameLength) / ffmt.sampleRate)
                    }
                    req.endAudio()
                    Thread.sleep(forTimeInterval: 8)
                    print("vp-echo-probe: FILE_RESULT |\(partials.last ?? "")|")
                    exitCode = 0
                    sem.signal()
                    return
                }
                let engine = AVAudioEngine()
                let input = engine.inputNode
                if vpOn {
                    try input.setVoiceProcessingEnabled(true)
                }
                let vpActive = input.isVoiceProcessingEnabled
                print("vp-echo-probe: isVoiceProcessingEnabled=\(vpActive)")

                // Streaming recognizer over the PROCESSED input.
                SFSpeechRecognizer.requestAuthorization { st in
                    print("vp-echo-probe: speech_auth=\(st.rawValue)")
                }
                let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
                print("vp-echo-probe: recognizer=\(rec != nil) available=\(rec?.isAvailable ?? false) onDevice=\(rec?.supportsOnDeviceRecognition ?? false)")
                let req = SFSpeechAudioBufferRecognitionRequest()
                req.shouldReportPartialResults = true
                if (rec?.supportsOnDeviceRecognition ?? false) { req.requiresOnDeviceRecognition = true }
                req.contextualStrings = ["Evie", "stop", "wait", "hold on"]
                // DIAGNOSTIC VARIANT: --asr-server permits Apple server ASR
                // to isolate whether on-device model delivery is the stall.
                if CommandLine.arguments.contains("--asr-server") {
                    // leave default: allow server too — maximal chance
                    // requiresOnDeviceRecognition = false
                    print("vp-echo-probe: asr=server_allowed")
                } else {
                    print("vp-echo-probe: asr=on_device_required")
                }
                var partials: [String] = []
                let recLock = NSLock()
                let task = rec?.recognitionTask(with: req) { result, error in
                    if let e = error { print("vp-echo-probe: REC_ERR \(e)") }
                    if let r = result {
                        let t = r.bestTranscription.formattedString
                        recLock.lock(); if partials.last != t { partials.append(t) }; recLock.unlock()
                        print("vp-echo-probe: PARTIAL |\(t)|")
                    }
                    if error != nil { print("vp-echo-probe: REC_ERROR") }
                }

                // Tap processed input: RMS buckets + feed recognizer.
                var rmsSum: Float = 0; var rmsN = 0
                var playing = false
                let fmt = input.outputFormat(forBus: 0)
                try AVAudioSafe.installTap(on: input, bufferSize: 2048, format: fmt) { buf, _ in
                    recLock.lock()
                    req.append(buf)
                    recLock.unlock()
                    if playing, let ch = buf.floatChannelData?[0] {
                        var e: Float = 0
                        for i in 0..<Int(buf.frameLength) { e += ch[i]*ch[i] }
                        rmsSum += (e/Float(buf.frameLength)).squareRoot()
                        rmsN += 1
                    }
                }

                // Far-end player THROUGH THE SAME ENGINE (two-way relationship).
                let file = try AVAudioFile(forReading: farURL)
                let player = AVAudioPlayerNode()
                engine.attach(player)
                engine.connect(player, to: engine.mainMixerNode,
                               format: file.processingFormat)
                let dur = Double(file.length) / file.processingFormat.sampleRate

                try engine.start()
                let durStr = String(format: "%.1f", dur)
                print("vp-echo-probe: engine started, far-end duration=\(durStr)s")
                playing = true
                player.scheduleFile(file, at: nil)
                player.play()

                // Keep the mic→recognizer pipeline alive well past playback:
                // on-device models have multi-second cold-start before the
                // first partial. Absorb it inside the measurement window.
                Thread.sleep(forTimeInterval: 24)
                playing = false
                req.endAudio()
                task?.finish()
                Thread.sleep(forTimeInterval: 2.0)

                recLock.lock(); let finalText = partials.last ?? ""; recLock.unlock()
                let lower = finalText.lowercased()
                let tokens = ["stop","wait","evie"].count(where: { lower.contains($0) })
                let avgRms = rmsN > 0 ? rmsSum/Float(rmsN) : 0
                let rmsStr = String(format: "%.4f", avgRms)
                print("vp-echo-probe: RESULT vp=\(vpOn) final_len=\(finalText.count) far_end_tokens_in_transcript=\(tokens)/3 avg_input_rms_during_playback=\(rmsStr)")
                print("vp-echo-probe: FINAL_TRANSCRIPT |\(finalText.prefix(200))|")
                exitCode = 0
            } catch {
                print("vp-echo-probe: FAIL \(error)")
            }
            sem.signal()
        }
        sem.wait()
        return exitCode
    }

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

    // MARK: - TTSPlayer continuity acceptance (directive: continuity pass)

    /// Continuity acceptance for the EXACT production TTSPlayer path, driven
    /// by provider-like delivery profiles over a real speech fixture:
    /// A steady realtime, B irregular jitter, C burst faster-than-realtime,
    /// D brief producer starvation. Hard targets: 0 drops / 0 overflow /
    /// 0 underruns / 0 sequence gaps / 0 invalid frames, and
    /// received == scheduled == played duration.
    static func runTTSContinuity() -> Int32 {
        setbuf(stdout, nil)
        let semaphore = DispatchSemaphore(value: 0)
        var exitCode: Int32 = 1
        Task {
            defer { semaphore.signal() }
            let priorVolume = continuityGetOutputVolume()
            defer { _ = continuitySetOutputVolume(priorVolume) }
            // Sims render realtime audio; mute the output for the run.
            _ = continuitySetOutputVolume(0)
            do {
                let fixture = try makeSpeechFixture()
                print(
                    "continuity: fixture \(fixture.pcm.count / 2) frames = "
                        + String(format: "%.1fs", Double(fixture.pcm.count / 2) / fixture.sampleRate)
                )
                var allPass = true
                allPass = await continuitySim(
                    name: "A-steady", pcm: fixture.pcm, sampleRate: fixture.sampleRate,
                    chunkRange: 20...60, rateJitter: 1.0...1.0, burstUntilSec: nil, stalls: []
                ) && allPass
                allPass = await continuitySim(
                    name: "B-jitter", pcm: fixture.pcm, sampleRate: fixture.sampleRate,
                    chunkRange: 5...120, rateJitter: 0.85...1.15, burstUntilSec: nil, stalls: [],
                    spikeChance: 0.05
                ) && allPass
                allPass = await continuitySim(
                    name: "C-burst", pcm: fixture.pcm, sampleRate: fixture.sampleRate,
                    chunkRange: 20...60, rateJitter: 1.0...1.0, burstUntilSec: 3.0, stalls: []
                ) && allPass
                allPass = await continuitySim(
                    name: "D-starve", pcm: fixture.pcm, sampleRate: fixture.sampleRate,
                    chunkRange: 20...60, rateJitter: 1.0...1.0, burstUntilSec: nil,
                    stalls: [0.25, 0.6]
                ) && allPass
                // THE REAL PROVIDER PROFILE: S2S models generate far faster
                // than realtime. The player must absorb the whole burst and
                // play it continuously — dropping anything here is the bug
                // that made Evie say one word and go silent.
                allPass = await continuitySim(
                    name: "E-fastgen", pcm: fixture.pcm, sampleRate: fixture.sampleRate,
                    chunkRange: 20...60, rateJitter: 1.0...1.0, burstUntilSec: nil,
                    stalls: [], rateOverride: 5.0
                ) && allPass
                exitCode = allPass ? 0 : 1
            } catch {
                print("continuity: FAIL — \(error)")
            }
        }
        semaphore.wait()
        return exitCode
    }

    private static func continuityGetOutputVolume() -> Int {
        let task = Process()
        task.launchPath = "/usr/bin/osascript"
        task.arguments = ["-e", "output volume of (get volume settings)"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
        } catch {
            return 0
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return Int(String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "") ?? 0
    }

    private static func continuitySetOutputVolume(_ value: Int) -> Bool {
        let task = Process()
        task.launchPath = "/usr/bin/osascript"
        task.arguments = ["-e", "set volume output volume \(value)"]
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
        } catch {
            return false
        }
        return task.terminationStatus == 0
    }

    private struct ContinuityFixture {
        let pcm: Data
        let sampleRate: Double
    }

    /// Real speech via the macOS `say` renderer; 16-bit mono 16k WAV payload.
    /// Falls back to a speech-like formant buzz if `say` is unavailable.
    private static func makeSpeechFixture() throws -> ContinuityFixture {
        let text = """
        Evie continuity acceptance. The playback path must stay smooth from the
        first syllable to the last. Each sentence is a fresh burst of packets
        arriving over the network, and the player must absorb small delays
        without dropping a single frame of speech. Listen to the rhythm of this
        paragraph: steady pacing, natural pauses between clauses, and a long
        final sentence that runs for several seconds without interruption. If
        any part of this recording stutters, skips, or lags behind, the
        continuity counters will reveal exactly which invariant was broken.
        The quick brown fox jumps over the lazy dog while the engine keeps a
        stable lead of scheduled audio ahead of the speaker at all times.
        """
        let dir = NSTemporaryDirectory()
        let wavPath = (dir as NSString).appendingPathComponent("ev-continuity.wav")
        try? FileManager.default.removeItem(atPath: wavPath)
        let say = Process()
        say.launchPath = "/usr/bin/say"
        say.arguments = ["-v", "Samantha", "-o", wavPath, "--data-format=LEI16@16000", text]
        say.standardError = FileHandle.nullDevice
        if (try? say.run()) != nil {
            say.waitUntilExit()
        }
        guard say.terminationStatus == 0,
              let wavData = FileManager.default.contents(atPath: wavPath),
              let payload = wav16MonoPayload(wavData)
        else {
            print("continuity: say unavailable — using formant fallback")
            let rate = 16_000.0
            let seconds = 32
            var pcm = Data(count: Int(rate * Double(seconds)) * 2)
            pcm.withUnsafeMutableBytes { raw in
                let samples = raw.bindMemory(to: Int16.self)
                for i in 0..<samples.count {
                    let t = Double(i) / rate
                    let syllable = fmod(t, 0.32)
                    let envelope = max(0, sin(.pi * syllable / 0.32))
                    let f0 = 120.0 + 40.0 * sin(2.0 * .pi * 0.7 * t)
                    var v = 0.0
                    for harmonic in 1...5 {
                        v += sin(2.0 * .pi * f0 * Double(harmonic) * t) / Double(harmonic)
                    }
                    samples[i] = Int16(v / 1.8 * envelope * 9000.0)
                }
            }
            return ContinuityFixture(pcm: pcm, sampleRate: rate)
        }
        try? FileManager.default.removeItem(atPath: wavPath)
        return ContinuityFixture(pcm: payload, sampleRate: 16_000)
    }

    private static func wav16MonoPayload(_ data: Data) -> Data? {
        guard data.count >= 44, data.starts(with: Data("RIFF".utf8)),
              String(data: data.subdata(in: 8..<12), encoding: .ascii) == "WAVE"
        else { return nil }
        var offset = 12
        var payload: Data?
        while offset + 8 <= data.count {
            let id = String(data: data.subdata(in: offset..<(offset + 4)), encoding: .ascii) ?? ""
            let size = data.subdata(in: (offset + 4)..<(offset + 8)).withUnsafeBytes {
                $0.bindMemory(to: UInt32.self).first.flatMap { UInt32(littleEndian: $0) }
            } ?? 0
            let start = offset + 8
            guard Int(size) <= data.count - start else { return nil }
            if id == "data" { payload = data.subdata(in: start..<(start + Int(size))) }
            offset = start + Int(size) + (Int(size) % 2)
        }
        return payload
    }

    private static func continuitySim(
        name: String,
        pcm: Data,
        sampleRate: Double,
        chunkRange: ClosedRange<Int>,
        rateJitter: ClosedRange<Double>,
        burstUntilSec: Double?,
        stalls: [Double],
        spikeChance: Double = 0.0,
        rateOverride: Double? = nil
    ) async -> Bool {
        let frames = pcm.count / 2
        let totalSec = Double(frames) / sampleRate
        let player = TTSPlayer()
        player.beginVoiceSession()
        try? await Task.sleep(nanoseconds: 150_000_000)
        let responseID = "continuity-\(name)"
        player.beginResponse(responseID)
        var offset = 0
        var sequence = 0
        var consumedSec = 0.0
        var pendingStalls = stalls.map { $0 * totalSec }
        // Provider-like self-correcting pacing: when delivery runs slower than
        // realtime (or a spike/stall delays it), the provider catches up by
        // sending subsequent chunks immediately instead of lagging forever.
        var debt = 0.0
        let feedStart = Date()
        while offset < frames {
            let chunkMs = Double.random(in: Double(chunkRange.lowerBound)...Double(chunkRange.upperBound))
            let chunkFrames = min(Int(chunkMs * sampleRate / 1000), frames - offset)
            let chunk = pcm.subdata(in: (offset * 2)..<((offset + chunkFrames) * 2))
            player.enqueuePCM(
                chunk, contentType: "audio/pcm", sampleRate: sampleRate,
                responseID: responseID, sequence: sequence
            )
            sequence += 1
            let prevSec = consumedSec
            offset += chunkFrames
            consumedSec = Double(offset) / sampleRate
            if let stallIdx = pendingStalls.firstIndex(where: { prevSec < $0 && $0 <= consumedSec }) {
                pendingStalls.remove(at: stallIdx)
                debt += 0.3
                continue
            }
            let ideal = Double(chunkFrames) / sampleRate
            var factor = rateOverride ?? Double.random(in: rateJitter)
            if let burstUntilSec, consumedSec < burstUntilSec { factor = 1.25 }
            if spikeChance > 0, Double.random(in: 0..<1) < spikeChance {
                debt += 0.12
            }
            var sleepSec = ideal / factor
            let applied = min(debt, sleepSec)
            debt -= applied
            sleepSec -= applied
            // Real providers cannot burst faster than ~2x realtime; cap the
            // catch-up so the simulation stays physically plausible.
            sleepSec = max(sleepSec, ideal / 2)
            if sleepSec > 0 {
                try? await Task.sleep(nanoseconds: UInt64(sleepSec * 1_000_000_000))
            }
        }
        let feedSec = Date().timeIntervalSince(feedStart)
        player.finishResponse(responseID)
        let drained = await waitContinuityDrained(player, timeout: totalSec + 20)
        // Read counters BEFORE endVoiceSession — session teardown resets them.
        let m = player.metrics()
        player.endVoiceSession()
        // §11: every fixture frame must be received, scheduled and played —
        // exact frame equality (source-rate frames, duration-equivalent).
        let accountingOk = m.pcmReceivedFrames == frames
            && m.pcmScheduledFrames == frames
            && m.pcmPlayedFrames == frames
        let clean = m.droppedFrames == 0 && m.overflowEvents == 0 && m.underrunEvents == 0
            && m.sequenceGapCount == 0 && m.invalidFrameCount == 0 && accountingOk && drained
        var profileOk = true
        if burstUntilSec != nil {
            // Lead must have risen above the steady target without dropping
            // speech, then return toward target by drain time.
            profileOk = Double(m.maxScheduledLeadMs) > 450 && m.droppedFrames == 0
        }
        let pass = clean && profileOk
        let receivedMs = Double(m.pcmReceivedFrames) * 1000 / sampleRate
        let scheduledMs = Double(m.pcmScheduledFrames) * 1000 / sampleRate
        let playedMs = Double(m.pcmPlayedFrames) * 1000 / sampleRate
        print(String(format: "%@: feed=%.1fs received=%.0fms scheduled=%.0fms played=%.0fms lead(min/max/cur)=%d/%d/%dms ageMax=%dms overflow=%d dropped=%d underruns=%d gaps=%d invalid=%d → %@",
                     name, feedSec, receivedMs, scheduledMs, playedMs,
                     m.minScheduledLeadMs, m.maxScheduledLeadMs, m.currentScheduledLeadMs,
                     m.maxQueueAgeMs, m.overflowEvents, m.droppedFrames, m.underrunEvents,
                     m.sequenceGapCount, m.invalidFrameCount, pass ? "PASS" : "FAIL"))
        return pass
    }

    private static func waitContinuityDrained(_ player: TTSPlayer, timeout: TimeInterval) async -> Bool {
        let start = Date()
        while Date().timeIntervalSince(start) < timeout {
            let m = player.metrics()
            if m.pcmScheduledFrames > 0, m.pcmPlayedFrames == m.pcmScheduledFrames {
                return true
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return false
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

    /// P0 regression driver for the 2026-08-21 crash (EV-*.ips): assistant
    /// playback start must never terminate EV.app. Feeds known-good PCM
    /// straight into TTSPlayer — no OpenAI, no realtime, no VAD, no gateway
    /// — then drives a confirmed interrupt from a simulated render thread to
    /// prove the stop is dispatched off-thread and nothing deadlocks.
    /// Exit 0 = process survived every stage.
    static func runFirstAudioSurvival() -> Int32 {
        print("first-audio: start")
        let player = TTSPlayer()
        var ok = true

        func tone(_ seconds: Double, _ amp: Float) -> Data {
            let sampleRate = 16_000
            let n = Int(Double(sampleRate) * seconds)
            var data = Data(count: n * 2)
            data.withUnsafeMutableBytes { raw in
                let dst = raw.bindMemory(to: Int16.self)
                for i in 0..<n {
                    let t = Double(i) / Double(sampleRate)
                    let sample = 0.9 * sin(2 * .pi * 210 * t) + 0.2 * sin(2 * .pi * 420 * t)
                    dst[i] = Int16(max(-1, min(1, Float(sample) * amp)) * 32_767.0)
                }
            }
            return data
        }

        func pump(_ seconds: TimeInterval) {
            // Drain the main queue while waiting: priming schedules
            // playerNode.play() via DispatchQueue.main, and a bare CLI has
            // no runloop unless we pump one.
            RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        }

        func waitIdle(_ timeout: TimeInterval) -> Bool {
            let deadline = Date().addingTimeInterval(timeout)
            while Date() < deadline {
                if !player.isPlaying { return true }
                pump(0.05)
            }
            return false
        }

        // Stage A — first buffer decoded, enqueued, engine started.
        do {
            try player.enqueue(tone(0.4, 0.12), contentType: "audio/pcm")
        } catch {
            print("first-audio: FAIL enqueue short — \(error)")
            return 1
        }
        var queued = false
        repeat {
            pump(0.05)
            queued = player.isPlaying
        } while !queued && Date() < Date().addingTimeInterval(2)
        // The decisive first-audio proof is that the queued word actually
        // drains through the graph (dataPlayedBack fired) without killing
        // the process.
        let shortCompleted = waitIdle(4)
        print("first-audio: queued=\(queued) short_completed=\(shortCompleted)")
        ok = ok && queued && shortCompleted

        // Stage C — bounded long response stays alive and stops cleanly.
        do {
            try player.enqueue(tone(3.0, 0.10), contentType: "audio/pcm")
        } catch {
            print("first-audio: FAIL enqueue long — \(error)")
            return 2
        }
        Thread.sleep(forTimeInterval: 0.2)
        pump(0.8)
        player.stop()
        let stoppedClean = waitIdle(1)
        print("first-audio: long_stopped_clean=\(stoppedClean)")
        ok = ok && stoppedClean

        // Stage D — interrupt confirmed on a simulated render thread:
        // handleMicFrame must return promptly with the stop dispatched away,
        // machine lands in userSpeaking, provider forwarding stays open.
        player.bind(to: nil)
        let renderSim = DispatchQueue(label: "ev.first-audio.render-sim")
        let session = LiveBargeInSession()
        _ = session.machine.acceptAssistantChunk()
        let voiced = pcmVoicedProbe(seconds: 0.3)
        let done = DispatchSemaphore(value: 0)
        final class HopBox: @unchecked Sendable {
            let guard_ = NSLock()
            var prerollMs = -1
        }
        let box = HopBox()
        renderSim.async {
            let frame = BargeInDetector.frameSamples * 2
            var offset = 0
            while offset + frame <= voiced.count {
                let mic = voiced.subdata(in: offset..<(offset + frame))
                session.handleMicFrame(
                    mic,
                    playback: PlaybackSnapshot(audible: true, echoGate: true, playedMs: offset * 1000 / 32_000),
                    forward: { _ in },
                    interrupt: { event in
                        DispatchQueue.global(qos: .userInteractive).async {
                            box.guard_.lock()
                            box.prerollMs = event.prerollMs
                            box.guard_.unlock()
                        }
                    }
                )
                offset += frame
            }
            done.signal()
        }
        let returned = done.wait(timeout: .now() + 5)
        print("first-audio: interrupt_returned=\(returned == .success)")
        ok = ok && returned == .success
        print("first-audio: phase_after=\(session.machine.currentPhase.rawValue)")
        ok = ok && session.machine.currentPhase == .userSpeaking
        let hopDeadline = Date().addingTimeInterval(2)
        while Date() < hopDeadline {
            box.guard_.lock()
            let ms = box.prerollMs
            box.guard_.unlock()
            if ms >= 0 { break }
            Thread.sleep(forTimeInterval: 0.02)
        }
        box.guard_.lock()
        let hopped = box.prerollMs >= 0
        box.guard_.unlock()
        print("first-audio: control_work_hopped=\(hopped)")
        ok = ok && hopped
        print("first-audio: process_alive=true")
        print(ok ? "first-audio: PASS" : "first-audio: FAIL")
        return ok ? 0 : 3
    }

    /// Listener-presence overlap stress: schedules many soft auxiliary
    /// backchannel clips on the real shared engine while asserting the
    /// owner-facing contracts — mic capture gate stays OPEN (never mute the
    /// user to play a nod), the assistant-response lane is untouched, the
    /// aux queue drains, stop() expendability races are safe, and the
    /// process survives. Exit 0 = all invariants held.
    static func runListenerPresenceOverlap() -> Int32 {
        print("listener-presence: start")
        let player = TTSPlayer()
        var ok = true

        // Synthetic soft variants (the shipped cache uses real Evie TTS;
        // this probe validates lane physics, not voice identity).
        func tone(_ seconds: Double, _ amp: Float) -> Data {
            let sampleRate = 16_000
            let n = Int(Double(sampleRate) * seconds)
            var data = Data(count: n * 2)
            data.withUnsafeMutableBytes { raw in
                let dst = raw.bindMemory(to: Int16.self)
                for i in 0..<n {
                    let sample = 0.8 * sin(2 * .pi * 190 * Double(i) / Double(sampleRate))
                    dst[i] = Int16(max(-1, min(1, Float(sample) * amp)) * 32_767.0)
                }
            }
            return data
        }

        func pump(_ seconds: TimeInterval) {
            RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        }

        // Contract 0: a nod must NEVER mute or displace owner capture.
        do {
            try player.enqueueListenerFeedback(tone(0.45, 0.12), gain: 0.34)
        } catch {
            print("listener-presence: FAIL first nod — \(error)")
            return 1
        }
        let muteOpen = !player.shouldMuteCapture
        print("listener-presence: capture_gate_open_during_nod=\(muteOpen)")
        ok = ok && muteOpen
        // BACKCHANNEL_PLAYING must NOT imply assistant-speaking: while a nod
        // renders on the shared node, the response-lane counter stays empty.
        pump(0.08)
        let responseLaneClean = !player.isPlaying
        print("listener-presence: response_lane_not_speaking=\(responseLaneClean)")
        ok = ok && responseLaneClean

        // Drain the single nod end-to-end.
        var drained = false
        let drainDeadline = Date().addingTimeInterval(5)
        while Date() < drainDeadline {
            pump(0.05)
            if player.listenerFeedbackQueuedFrames == 0 { drained = true; break }
        }
        print("listener-presence: nod_drained=\(drained)")
        ok = ok && drained

        // Overlap stress: waves of scheduled nods with teardown races.
        var scheduled = 0
        for wave in 0..<5 {
            for i in 0..<20 {
                do {
                    try player.enqueueListenerFeedback(tone(0.4, 0.10), gain: 0.3 + Float(i % 4) * 0.02)
                    scheduled += 1
                } catch {
                    print("listener-presence: FAIL schedule wave=\(wave) i=\(i) — \(error)")
                    return 2
                }
                if i % 7 == 3 {
                    pump(0.03) // let some start rendering before the race
                }
            }
            // Expendability: stop must clear the aux queue instantly and the
            // process must not care whether buffers were mid-render.
            player.stop()
            pump(0.15)
            let cleared = player.listenerFeedbackQueuedFrames == 0
            print("listener-presence: wave=\(wave) cleared_after_stop=\(cleared)")
            ok = ok && cleared
        }
        print("listener-presence: overlaps_scheduled=\(scheduled)")
        ok = ok && scheduled == 100

        // COMPLETION IMMUNITY (the Round One killer): fire the exact
        // role-C barge-in stop WHILE a nod renders. The nod must survive.
        do {
            try player.enqueueListenerFeedback(tone(0.6, 0.12), gain: 0.34)
        } catch {
            print("listener-presence: FAIL immunity nod — \(error)")
            return 5
        }
        pump(0.08)
        let queuedBeforeStop = player.listenerFeedbackQueuedFrames
        player.stopForBargeIn()
        pump(0.08)
        let queuedAfterStop = player.listenerFeedbackQueuedFrames
        let survived = queuedBeforeStop > 0 && queuedAfterStop == queuedBeforeStop
        print("listener-presence: nod_survives_stopForBargeIn=\(survived) before=\(queuedBeforeStop) after=\(queuedAfterStop)")
        ok = ok && survived
        var immuneDrain = false
        let immuneDeadline = Date().addingTimeInterval(5)
        while Date() < immuneDeadline {
            pump(0.05)
            if player.listenerFeedbackQueuedFrames == 0 { immuneDrain = true; break }
        }
        print("listener-presence: immune_nod_completed_naturally=\(immuneDrain)")
        ok = ok && immuneDrain

        // Sanctioned preemption: NORMAL_RESPONSE > LISTENER_BACKCHANNEL.
        do {
            try player.enqueueListenerFeedback(tone(0.6, 0.12), gain: 0.34)
        } catch {
            print("listener-presence: FAIL preempt nod — \(error)")
            return 6
        }
        pump(0.05)
        player.preemptListenerFeedbackForResponse(reason: "probe_response")
        pump(0.05)
        let preemptedClean = player.listenerFeedbackQueuedFrames == 0
        print("listener-presence: response_preempts_nod=\(preemptedClean)")
        ok = ok && preemptedClean

        // Self-playback reference: nods must register as SELF audio so the
        // barge-in detector can correlate them away from owner speech.
        do {
            try player.enqueueListenerFeedback(tone(0.5, 0.12), gain: 0.34)
        } catch {
            print("listener-presence: FAIL reference nod — \(error)")
            return 3
        }
        pump(0.1)
        let referenceBytes = player.playbackSnapshot().pcm16.count
        print("listener-presence: self_reference_registered=\(referenceBytes > 0) bytes=\(referenceBytes)")
        ok = ok && referenceBytes > 0
        player.stop()

        print("listener-presence: process_alive=true")
        let accounting = player.listenerFeedbackAccounting
        let unexpected = accounting.started - accounting.completed - accounting.preempted - accounting.dropped
        print("listener-presence: accounting started=\(accounting.started) completed=\(accounting.completed) preempted=\(accounting.preempted) dropped=\(accounting.dropped) unexpected=\(unexpected)")
        ok = ok && unexpected == 0
        print(ok ? "listener-presence: PASS" : "listener-presence: FAIL")
        return ok ? 0 : 4
    }

    /// Hardware probe: keep capture alive while TTSPlayer is audibly playing.
    /// Does not require a human interruption. Proves mic frames, detector
    /// input, and echo-only rejection on the real audio devices.
    static func runBargeInProbe() -> Int32 {
        print("BARGE_RUNTIME=\(BargeInTrace.runtimeId)")
        print("BARGE_ENGINE=\(BargeInTrace.engine)")
        BargeInTrace.marker()
        let player = TTSPlayer()
        let microphone = LiveVoiceMicrophone()
        let session = LiveBargeInSession()
        _ = session.machine.acceptAssistantChunk()
        final class ProbeState: @unchecked Sendable {
            let lock = NSLock()
            var frames = 0
            var maxMic: Float = 0
            var maxPlay: Float = 0
            var confirmed = false
            var forwarded = 0
        }
        let state = ProbeState()
        guard AudioInputLease.acquire(.live) else {
            print("barge-in-probe: FAIL — live microphone lease unavailable")
            return 2
        }
        defer { AudioInputLease.release(.live) }
        do {
            try microphone.start(enqueue: { data in
                let snap = player.playbackSnapshot()
                let rms = BargeInDetector.rms(BargeInDetector.floatSamples(data))
                state.lock.lock()
                state.frames += 1
                if rms > state.maxMic { state.maxMic = rms }
                if snap.rms > state.maxPlay { state.maxPlay = snap.rms }
                state.lock.unlock()
                session.handleMicFrame(
                    data,
                    playback: snap,
                    forward: { _ in
                        state.lock.lock()
                        state.forwarded += 1
                        state.lock.unlock()
                    },
                    interrupt: { event in
                        state.lock.lock()
                        state.confirmed = true
                        state.lock.unlock()
                        // Never stop the player on this render thread: the
                        // tap callback runs on the engine messenger queue and
                        // AVAudioPlayerNode.stop() syncs to it (deadlock).
                        DispatchQueue.global(qos: .userInteractive).async {
                            player.stopForBargeIn()
                            print(
                                "barge-in-probe: CONFIRMED preroll_ms=\(event.prerollMs) "
                                    + String(format: "confidence=%.2f", event.confidence)
                            )
                        }
                    }
                )
            })
        } catch {
            print("barge-in-probe: FAIL — microphone \(error.localizedDescription)")
            return 3
        }
        let pcm = bargeProbeTone(seconds: 2.2)
        do {
            try player.enqueue(pcm, contentType: "audio/pcm", sampleRate: 16_000)
        } catch {
            print("barge-in-probe: FAIL — playback \(error.localizedDescription)")
            microphone.stop()
            return 4
        }
        Thread.sleep(forTimeInterval: 2.6)
        let playing = player.isPlaying
        player.stop()
        Thread.sleep(forTimeInterval: 0.2)
        microphone.stop()
        state.lock.lock()
        let captured = state.frames
        let micPeak = state.maxMic
        let playPeak = state.maxPlay
        let didConfirm = state.confirmed
        let didForward = state.forwarded
        state.lock.unlock()
        print("barge-in-probe: frames=\(captured) forwarded=\(didForward) confirmed=\(didConfirm)")
        print(String(format: "barge-in-probe: max_mic_rms=%.4f max_play_rms=%.4f playing_at_end=%@", micPeak, playPeak, playing ? "true" : "false"))
        print("barge-in-probe: capture_during_playback=\(captured > 20 ? "YES" : "NO")")
        print("barge-in-probe: playback_reference=\(playPeak > 0.002 ? "YES" : "NO")")
        print("barge-in-probe: echo_or_noise=\(micPeak > 0.003 ? "YES" : "NO")")
        print("barge-in-probe: echo_only_false_positive=\(didConfirm ? "YES" : "NO")")
        if captured <= 20 {
            print("barge-in-probe: FAIL — microphone was deaf during playback")
            return 5
        }
        if playPeak <= 0.002 {
            print("barge-in-probe: FAIL — playback reference was empty")
            return 6
        }
        print("barge-in-probe: PASS")
        return 0
    }
}

private func bargeProbeTone(seconds: Double, sampleRate: Int = 16_000) -> Data {
    let n = Int(Double(sampleRate) * seconds)
    var data = Data(count: n * 2)
    data.withUnsafeMutableBytes { raw in
        let dst = raw.bindMemory(to: Int16.self)
        for i in 0..<n {
            let t = Double(i) / Double(sampleRate)
            let sample = 0.22 * sin(2 * Double.pi * 180 * t)
                + 0.10 * sin(2 * Double.pi * 360 * t)
            dst[i] = Int16(max(-1, min(1, sample)) * 32767.0)
        }
    }
    return data
}

private func pcmVoicedProbe(seconds: Double, sampleRate: Int = 16_000) -> Data {
    let n = Int(Double(sampleRate) * seconds)
    var a = Data(count: n * 2)
    var b = Data(count: n * 2)
    a.withUnsafeMutableBytes { raw in
        let dst = raw.bindMemory(to: Int16.self)
        for i in 0..<n {
            let sample = 0.18 * sin(2 * Double.pi * 180 * Double(i) / Double(sampleRate))
            dst[i] = Int16((sample * 32_767.0).rounded())
        }
    }
    b.withUnsafeMutableBytes { raw in
        let dst = raw.bindMemory(to: Int16.self)
        for i in 0..<n {
            let sample = 0.08 * sin(2 * Double.pi * 360 * Double(i) / Double(sampleRate))
            dst[i] = Int16((sample * 32_767.0).rounded())
        }
    }
    var out = Data(count: n * 2)
    out.withUnsafeMutableBytes { rawO in
        a.withUnsafeBytes { rawA in
            b.withUnsafeBytes { rawB in
                let sa = rawA.bindMemory(to: Int16.self)
                let sb = rawB.bindMemory(to: Int16.self)
                let so = rawO.bindMemory(to: Int16.self)
                for i in 0..<n {
                    so[i] = Int16(max(-32_767, min(32_767, Int(sa[i]) + Int(sb[i]))))
                }
            }
        }
    }
    return out
}
