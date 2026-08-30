import AVFoundation
import Foundation
import Speech

/// EVIE INTERRUPTION V1 — explicit-address natural barge-in (owner decision
/// 2026-08-23). While Evie is speaking, the owner takes the floor by
/// addressing her ("Evie…", "Hey Evie…") at normal volume.
///
/// COMPOSITION LAW: when the feature flag is off, nothing here is
/// constructed — no detector, no recognizer, no callbacks, no stop authority.
///
/// Detection branch (OPTION B — local streaming ASR + address evidence +
/// acoustic ownership fusion):
///   mic tap copy -> ring -> SFSpeechRecognizer (on-device partials)
///   + delay-aware correlation vs the player's own reference PCM
///   -> SELF / OWNER / AMBIGUOUS. Only OWNER_CONFIRMED may interrupt.
///
/// Realtime law: the mic tap calls ``ingest`` with ONLY bounded arithmetic
/// and ring appends. Speech callbacks run on Speech's queue; the confirmed
/// interruption is dispatched to a control queue — the audio thread never
/// touches the player graph, the connection, or files.
// ⚠️ DEAD / LEGACY / UNWIRED (2026-08-23 closure) — spoken interruption is
// CLOSED. This file is retained for historical reference only. It is NOT
// constructed, attached, or invoked anywhere in production. Do not wire it
// back without a NEW architecture initiative (see MAC_VOICE_BASELINE.md).

final class ExplicitInterruptMonitor {
    static let flagKey = "EV_EXPLICIT_INTERRUPT_ENABLED"

    private let sampleRate = 16_000.0
    private let windowBytes: Int
    private let prerollBytes: Int
    private let maxLagSamples = 7_200 // 450 ms speaker->room->mic delay bound
    private let selfCorrReject: Float = 0.5
    private let selfCorrClear: Float = 0.35
    private let persistenceNeeded = 2

    private var window = Data()
    private var preroll = Data()
    private var latestReference = Data()
    private var armed = false
    private var latched = false
    private var addressHits = 0
    private var startedAt: CFAbsoluteTime?
    private var lastPartialTrace: CFAbsoluteTime = 0

    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    /// INTERRUPTION V2 GRAMMAR (owner decision 2026-08-23): two classes.
    /// CLASS 1 — direct floor-take commands, no address required:
    /// "Stop." "Wait." "Hold on." "Pause." "Enough." "No." "Cancel that."
    /// CLASS 2 — explicit address: "Evie…" / "Hey Evie…".
    /// Both are ANCHORED to the utterance start so Evie saying
    /// "the stop sign" or "my name is Evie" mid-sentence cannot match.
    /// Ownership fusion (SELF/OWNER/AMBIGUOUS) still gates every confirm.
    private let addressRegex = try! NSRegularExpression(
        pattern: "^\\W*(hey\\s+|ok(?:ay)?\\s+|hi\\s+|hello\\s+)?(evie|evy|evi|ee\\s?vee)\\b",
        options: [.caseInsensitive]
    )
    private let commandRegex = try! NSRegularExpression(
        pattern: "^\\W*(?:please\\s+)?(stop(\\s+(talking|it|now))?|wait(\\s+a?\\s*second)?|hold\\s+on|pause|enough|no|cancel\\s+that|hang\\s+on)\\b",
        options: [.caseInsensitive]
    )

    /// Executed on ``controlQueue`` when OWNER is confirmed — never on the
    /// audio thread. The closure owns: local stop, playback report, the
    /// barge-in control, and preroll forwarding.
    private let onConfirm: (_ playedMs: Int, _ preroll: Data) -> Void
    /// Lifecycle mirror into the host's PROVEN trace channel (startup-trace).
    /// The monitor's own writer was silent in the field; this removes doubt.
    private let lifecycle: (_ event: String, _ detail: String) -> Void

    private var latestPlayedMs = 0
    private let controlQueue: DispatchQueue
    private let traceQueue = DispatchQueue(label: "ev.interrupt-v1.trace", qos: .utility)
    private var traceFile: URL?

    init(
        controlQueue: DispatchQueue,
        onConfirm: @escaping (_ playedMs: Int, _ preroll: Data) -> Void,
        lifecycle: @escaping (_ event: String, _ detail: String) -> Void
    ) {
        self.lifecycle = lifecycle
        self.controlQueue = controlQueue
        self.onConfirm = onConfirm
        self.windowBytes = Int(0.9 * sampleRate) * 2
        self.prerollBytes = Int(1.6 * sampleRate) * 2
        let logs = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = logs.appendingPathComponent("Logs/EV", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        traceFile = dir.appendingPathComponent("interrupt-v1-trace.jsonl")
        lifecycle("IV_MONITOR_INIT", traceFile?.path ?? "nil")
        NSLog("INTV1 monitor INIT entered, traceFile=%@", traceFile?.path ?? "nil")
        // FORENSICS LAW (V3): construction and every authorization outcome
        // must be observable. The silent non-authorized return previously
        // erased all evidence of why interruption was inert.
        trace("INT00_CONSTRUCTED", ["flag_key": Self.flagKey])
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            guard let self else { return }
            let statusName: String
            switch status {
            case .authorized: statusName = "authorized"
            case .denied: statusName = "denied"
            case .restricted: statusName = "restricted"
            case .notDetermined: statusName = "not_determined"
            @unknown default: statusName = "unknown_\(status.rawValue)"
            }
            guard status == .authorized else {
                self.trace("INT_RECOG_AUTH_DENIED", ["status": statusName])
                self.lifecycle("IV_AUTH", statusName)
                return
            }
            let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
            self.trace("INT00_ARMED", [
                "on_device": rec?.supportsOnDeviceRecognition ?? false,
                "available": rec?.isAvailable ?? false,
            ])
            self.lifecycle("IV_AUTH_AUTHORIZED",
                           "on_device=\(rec?.supportsOnDeviceRecognition ?? false)")
            self.recognizer = rec
        }
    }

    // MARK: lifecycle (main actor / control side)

    /// Assistant playback began: arm detection and reset the per-episode latch.
    func arm() {
        controlQueue.async { [weak self] in
            guard let self else { return }
            self.latched = false
            self.addressHits = 0
            self.armed = true
            self.startedAt = CFAbsoluteTimeGetCurrent()
            self.startRecognition()
            self.trace("INT01_ARMED_PLAYBACK", [:])
        }
    }

    func disarm() {
        controlQueue.async { [weak self] in
            self?.armed = false
            self?.endRecognition()
        }
    }

    // MARK: mic tap copy (audio thread — bounded work only)

    /// ``snapshot`` carries the player's own reference PCM (SELF evidence)
    /// and the authoritative played position; safe from the tap thread.
    func ingest(_ pcm16: Data, snapshot: (pcm16: Data, playedMs: Int)?) {
        guard armed, !latched, pcm16.count > 0 else { return }
        if let snapshot {
            latestReference = snapshot.pcm16
            latestPlayedMs = snapshot.playedMs
        }
        window.append(pcm16)
        preroll.append(pcm16)
        if window.count > windowBytes { window.removeFirst(window.count - windowBytes) }
        if preroll.count > prerollBytes { preroll.removeFirst(preroll.count - prerollBytes) }
        append(toRecognition: pcm16)
    }

    // MARK: ownership fusion (Speech callback queue)

    struct Ownership {
        let classification: String  // SELF | OWNER | AMBIGUOUS
        let residualRatio: Float
        let gain: Float
    }

    /// Ownership fusion — FINAL ARCHITECTURE (V3, evidence-driven).
    ///
    /// Proven from the instrumented owner session:
    ///  1. ASR transcribes BOTH voices; owner commands land appended to a
    ///     transcript dominated by Evie's echo → ^-anchored matching on the
    ///     full transcript can never fire (CASE B). Fix: TAIL-WINDOW match.
    ///  2. Raw same-window correlation returned 0.0 always (reference ring
    ///     shorter than analysis window) → SELF veto never fired (CASE C).
    ///     Fix: matched-filter RESIDUAL ownership against a real 4 s far-end
    ///     reference (double-talk detection): if a delayed+gain-fitted copy
    ///     of Evie's own audio explains the window, it is SELF; what remains
    ///     is the OWNER component.
    private func handlePartial(_ text: String) {
        guard armed, !latched else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // IV07/IV08 FORENSICS: raw partials, throttled.
        let now = CFAbsoluteTimeGetCurrent()
        if now - lastPartialTrace >= 0.7 {
            lastPartialTrace = now
            let own = ownership()
            trace("IV_PARTIAL", [
                "text": String(trimmed.suffix(160)),
                "class": own.classification,
                "residual_ratio": own.residualRatio,
                "gain": own.gain,
            ])
        }
        // TAIL-WINDOW MATCH: evaluate only the recent tail of the transcript
        // (last ~48 chars). Evie's earlier sentence content cannot bury or
        // gate the owner's command; her OWN tail speech is still caught by
        // the ownership veto below.
        let tail = String(trimmed.suffix(48)).lowercased()
        let isAddress = tail.contains("evie") || tail.contains("evy") || tail.contains("evi")
        let commandWords = ["stop", "wait", "hold on", "hang on", "pause", "enough", "cancel that"]
        let isCommand = !isAddress && commandWords.contains { tail.contains($0) }
        guard isAddress || isCommand else {
            addressHits = 0
            return
        }
        let own = ownership()
        if own.classification == "SELF" {
            trace("INT03_SELF_ECHO", ["corr": own.residualRatio, "gain": own.gain, "partial": trimmed])
            addressHits = 0
            return
        }
        if own.classification == "AMBIGUOUS" {
            trace("INT04_AMBIGUOUS", ["residual": own.residualRatio, "gain": own.gain, "partial": trimmed])
            return // gather more evidence; never interrupt on ambiguous
        }
        addressHits += 1
        trace(isCommand ? "INT02_COMMAND_CANDIDATE" : "INT02_ADDRESS_CANDIDATE",
              ["residual": own.residualRatio, "hits": addressHits, "partial": trimmed])
        // One-word fast path: a command with firmly-independent residual
        // confirms on the first hit; otherwise two consecutive hits.
        if addressHits >= persistenceNeeded || (isCommand && own.residualRatio >= 0.55) {
            confirm(partial: trimmed, corr: own.residualRatio)
        }
    }

    /// Double-talk ownership via delay-aware matched filter + RESIDUAL:
    /// find the delayed copy of Evie's reference that best explains the mic
    /// window, fit gain in closed form; remaining energy is whatever the
    /// playback cannot explain. Echo-only → tiny residual → SELF.
    /// Owner over Evie → substantial independent residual → OWNER.
    private func ownership() -> Ownership {
        let mic = samples(window)
        let ref = samples(latestReference)
        guard mic.count >= 1600, ref.count >= 1600 else {
            return Ownership(classification: "AMBIGUOUS", residualRatio: 1, gain: 0)
        }
        var bestResidual = Float.infinity
        var bestGain: Float = 0
        var lag = 0
        while lag <= maxLagSamples {
            let end = ref.count - lag
            guard end >= mic.count else { break }
            let seg = Array(ref[(end - mic.count)..<end])
            var refEnergy: Float = 0
            var dot: Float = 0
            for i in 0..<mic.count {
                refEnergy += seg[i] * seg[i]
                dot += mic[i] * seg[i]
            }
            guard refEnergy > 1e-7 else { lag += 320; continue }
            let gain = max(0, min(1.4, dot / refEnergy))
            var resEnergy: Float = 0
            for i in 0..<mic.count {
                let d = mic[i] - gain * seg[i]
                resEnergy += d * d
            }
            let residual = (resEnergy / Float(mic.count)).squareRoot()
            if residual < bestResidual {
                bestResidual = residual
                bestGain = gain
            }
            lag += 320
        }
        let micRms = sqrtf(Float(mic.count) > 0 ? norm(mic) * norm(mic) / Float(mic.count) : 0)
        guard bestResidual.isFinite, micRms > 1e-6 else {
            return Ownership(classification: "AMBIGUOUS", residualRatio: 1, gain: 0)
        }
        let ratio = bestResidual / micRms
        // Bands calibrated on incident evidence: echo-explained windows leave
        // ≤~25% residual; genuine double-talk leaves most unexplained.
        if bestGain >= 0.08, ratio <= 0.25 {
            return Ownership(classification: "SELF", residualRatio: ratio, gain: bestGain)
        }
        if ratio >= 0.55 {
            return Ownership(classification: "OWNER", residualRatio: ratio, gain: bestGain)
        }
        return Ownership(classification: "AMBIGUOUS", residualRatio: ratio, gain: bestGain)
    }

    private func confirm(partial: String, corr: Float) {
        latched = true
        let playedMs = latestPlayedMs
        let prerollCopy = preroll
        let elapsed = startedAt.map { Int((CFAbsoluteTimeGetCurrent() - $0) * 1000) } ?? -1
        trace("INT05_OWNER_CONFIRMED", ["corr": corr, "played_ms": playedMs, "arm_elapsed_ms": elapsed, "partial": partial])
        controlQueue.async { [weak self] in
            guard let self else { return }
            self.endRecognition()
            self.onConfirm(playedMs, prerollCopy)
        }
    }

    // MARK: correlation vs the assistant's own reference (SELF evidence)

    private func ownershipCorrelation() -> Float {
        let mic = samples(window)
        let ref = samples(latestReference.suffix(Int(3.0 * sampleRate) * 2))
        guard mic.count >= 1600, ref.count >= 1600 else { return 0 }
        let micMeanRemoved = centered(mic)
        let micNorm = norm(micMeanRemoved)
        guard micNorm > 1e-6 else { return 0 }
        var best: Float = 0
        var lag = 0
        while lag <= maxLagSamples {
            let end = ref.count - lag
            guard end >= micMeanRemoved.count else { break }
            let seg = Array(ref[(end - micMeanRemoved.count)..<end])
            let segC = centered(seg)
            let denom = micNorm * norm(segC)
            if denom > 1e-9 {
                let c = abs(dot(micMeanRemoved, segC) / denom)
                if c > best { best = c }
            }
            lag += 320 // 20 ms steps
        }
        return min(best, 1)
    }

    // MARK: streaming recognition

    private func startRecognition() {
        guard task == nil, let recognizer, recognizer.isAvailable else { return }
        // LAW: interruption detection must not depend on a cloud round-trip.
        // If this Mac cannot run speech recognition on-device, V1 stays
        // inert rather than silently shipping audio to a server.
        guard recognizer.supportsOnDeviceRecognition else {
            trace("INT_RECOG_UNAVAILABLE", ["reason": "no_on_device_support"])
            return
        }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = true
        // Directive-sanctioned short contextual hints (no sentence stuffing).
        req.contextualStrings = ["Evie", "stop", "wait", "hold on", "pause"]
        request = req
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            if let result {
                self.handlePartial(result.bestTranscription.formattedString)
                if result.isFinal { self.restartSoon() }
            } else if let error {
                self.trace("INT_RECOG_ERROR", ["error": error.localizedDescription])
                self.restartSoon()
            }
        }
    }

    private func restartSoon() {
        task = nil
        request = nil
        controlQueue.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            guard let self, self.armed, !self.latched else { return }
            self.startRecognition()
        }
    }

    private func endRecognition() {
        task?.cancel()
        task = nil
        request?.endAudio()
        request = nil
    }

    private func append(toRecognition pcm16: Data) {
        guard let request else { return }
        let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
        let frames = AVAudioFrameCount(pcm16.count / 2)
        guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: frames) else { return }
        buf.frameLength = frames
        if let dst = buf.floatChannelData?[0] {
            pcm16.withUnsafeBytes { raw in
                let src = raw.bindMemory(to: Int16.self)
                for i in 0..<Int(frames) { dst[i] = Float(Int16(littleEndian: src[i])) / 32768.0 }
            }
        }
        request.append(buf)
    }

    // MARK: trace (async, bounded)

    private func trace(_ event: String, _ fields: [String: Any]) {
        // PRIMARY SINK: the host's proven startup-trace channel (works).
        var flat = ""
        for (k, v) in fields.sorted(by: { "\($0.key)" < "\($1.key)" }) {
            flat += " \(k)=\(v)"
        }
        lifecycle(event, flat.trimmingCharacters(in: .whitespaces))
        // SECONDARY SINK: dedicated jsonl (create-first fixed); best-effort.
        guard let traceFile else { return }
        var payload: [String: Any] = ["event": event, "ts_ms": Int(Date().timeIntervalSince1970 * 1000)]
        payload.merge(fields) { _, new in new }
        traceQueue.async { [weak self] in
            guard let self, let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
            // CREATE-FIRST LAW: FileHandle(forWritingTo:) does NOT create the
            // file. The missing createFile made this entire trace sink a
            // silent no-op since birth — hiding exactly the forensics the
            // V2 physical failure needed.
            if !FileManager.default.fileExists(atPath: self.traceFile!.path) {
                FileManager.default.createFile(atPath: self.traceFile!.path, contents: nil)
            }
            if let handle = try? FileHandle(forWritingTo: self.traceFile!) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data + Data("\n".utf8))
            }
        }
    }

    // MARK: small numeric helpers

    private func samples(_ data: Data) -> [Float] {
        let count = data.count / 2
        var out = [Float](repeating: 0, count: count)
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let src = raw.bindMemory(to: Int16.self)
            for i in 0..<count {
                let v = Int16(littleEndian: src[i])
                out[i] = Float(v) / 32768.0
            }
        }
        return out
    }

    private func centered(_ v: [Float]) -> [Float] {
        let count = max(v.count, 1)
        var total: Float = 0
        for x in v { total += x }
        let mean = total / Float(count)
        var out = v
        for i in out.indices { out[i] = out[i] - mean }
        return out
    }

    private func norm(_ v: [Float]) -> Float {
        var total: Float = 0
        for x in v {
            let prod: Float = x * x
            total += prod
        }
        return sqrtf(total)
    }

    private func dot(_ a: [Float], _ b: [Float]) -> Float {
        var total: Float = 0
        for i in 0..<min(a.count, b.count) {
            let prod: Float = a[i] * b[i]
            total += prod
        }
        return total
    }
}
