import AVFoundation
import Combine
import Foundation

/// What the always-on loop is doing, mirrored from the server's `state` events
/// so the menu bar never claims something the backend is not actually doing.
enum HandsFreeState: Sendable {
    case off
    case connecting
    case idle
    case waking
    case listening
    case thinking
    case speaking
    case followUp

    /// Maps the state strings emitted by `app/voice/live.py`.
    init?(serverState: String) {
        switch serverState {
        case "idle": self = .idle
        case "waking": self = .waking
        case "listening": self = .listening
        case "thinking": self = .thinking
        case "speaking": self = .speaking
        case "follow_up": self = .followUp
        case "closed": self = .off
        default: return nil
        }
    }

    var label: String {
        switch self {
        case .off: return "off"
        case .connecting: return "connecting…"
        case .idle: return "listening for “EVIE”"
        case .waking: return "heard you"
        case .listening: return "listening"
        case .thinking: return "thinking"
        case .speaking: return "speaking"
        case .followUp: return "go ahead"
        }
    }
}

enum HandsFreeError: LocalizedError {
    case noInputDevice
    case converterUnavailable
    case engineStart(String)
    case playbackRefused

    var errorDescription: String? {
        switch self {
        case .noInputDevice:
            return "No usable audio input device — pick a microphone in System Settings → Sound → Input."
        case .converterUnavailable:
            return "This microphone's format cannot be converted to 16 kHz mono."
        case .engineStart(let detail):
            return "The audio engine would not start: \(detail)"
        case .playbackRefused:
            return "The audio device refused to play the reply."
        }
    }
}

// --------------------------------------------------------------------------- #
// Capture
// --------------------------------------------------------------------------- #

/// Continuous microphone capture for the hands-free stream.
///
/// Uses `AVAudioEngine` rather than the `AVCaptureSession` in ``MicCapture``:
/// push-to-talk wants one blob at the end, this wants a tap it can convert and
/// ship frame by frame forever. An `AVAudioConverter` turns whatever the
/// hardware hands us into the 16 kHz mono PCM16 frames `WS /v1/voice/live`
/// expects; the converter is created once so its resampler keeps its state
/// across buffers.
final class HandsFreeMic: @unchecked Sendable {
    private let lock = NSLock()
    private var engine: AVAudioEngine?
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private var targetFormat: AVAudioFormat?
    private var pending = Data()
    private var frameBytes = 640
    private var handler: ((Data) -> Void)?

    var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return engine?.isRunning ?? false
    }

    /// Start streaming `frameSamples`-sized PCM16 frames at `sampleRate`.
    func start(
        sampleRate: Double,
        frameSamples: Int,
        onFrame: @escaping (Data) -> Void
    ) throws {
        stop()
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let hardware = input.outputFormat(forBus: 0)
        // A zero sample rate is what macOS reports when there is no input
        // device or the microphone is still blocked by TCC.
        guard hardware.sampleRate > 0, hardware.channelCount > 0 else {
            throw HandsFreeError.noInputDevice
        }
        guard
            let target = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: false
            ),
            let converter = AVAudioConverter(from: hardware, to: target)
        else {
            throw HandsFreeError.converterUnavailable
        }

        lock.lock()
        self.engine = engine
        self.converter = converter
        self.inputFormat = hardware
        self.targetFormat = target
        self.frameBytes = max(2, frameSamples * 2)
        self.pending = Data()
        self.handler = onFrame
        lock.unlock()

        input.installTap(onBus: 0, bufferSize: 4096, format: hardware) { [weak self] buffer, _ in
            self?.consume(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            stop()
            throw HandsFreeError.engineStart(error.localizedDescription)
        }
    }

    func stop() {
        lock.lock()
        let running = engine
        engine = nil
        converter = nil
        inputFormat = nil
        targetFormat = nil
        handler = nil
        pending = Data()
        lock.unlock()

        guard let running else { return }
        running.inputNode.removeTap(onBus: 0)
        if running.isRunning {
            running.stop()
        }
    }

    /// Called on the audio thread for every tap buffer.
    private func consume(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        guard let target = targetFormat, let handler else { return }
        if inputFormat != buffer.format {
            // The hardware changed under us; a converter built for the old
            // format would only produce garbage or errors.
            guard let rebuilt = AVAudioConverter(from: buffer.format, to: target) else { return }
            converter = rebuilt
            inputFormat = buffer.format
        }
        guard let converter else { return }

        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 64
        guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            return
        }

        var offered = false
        var error: NSError?
        // The converter drains the input block until the output buffer is full,
        // so the second call has to say "nothing more right now" instead of
        // handing back the same buffer and duplicating audio.
        let status = converter.convert(to: converted, error: &error) { _, inputStatus in
            if offered {
                inputStatus.pointee = .noDataNow
                return nil
            }
            offered = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error,
              converted.frameLength > 0,
              let channel = converted.int16ChannelData
        else {
            return
        }

        pending.append(Data(bytes: channel[0], count: Int(converted.frameLength) * 2))
        while pending.count >= frameBytes {
            let frame = Data(pending.prefix(frameBytes))
            pending.removeFirst(frameBytes)
            handler(frame)
        }
    }
}

// --------------------------------------------------------------------------- #
// Playback
// --------------------------------------------------------------------------- #

/// Reply playback for the hands-free loop.
///
/// Kept out of ``HandsFreeSession`` because both AVFoundation completion hooks
/// are `@objc` delegate callbacks that arrive off the main actor. This bridge
/// collapses them into one "finished" signal, which is exactly the moment the
/// server needs a `playback_finished` frame to open the follow-up window.
final class HandsFreePlayback: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    /// Fired when server audio or locally spoken text finished on its own.
    var onFinished: (@Sendable () -> Void)?

    private var player: AVAudioPlayer?
    private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func play(data: Data) throws {
        stop()
        let player = try AVAudioPlayer(data: data)
        player.delegate = self
        player.prepareToPlay()
        guard player.play() else {
            throw HandsFreeError.playbackRefused
        }
        self.player = player
    }

    /// Speak the reply with the system voice, for servers whose TTS provider
    /// only returns prosody metadata (`reply.speak_locally`).
    func speak(_ text: String) {
        stop()
        synthesizer.speak(AVSpeechUtterance(string: text))
    }

    /// Stop now and stay quiet: a barge-in or a shutdown means the server has
    /// already moved on, so completion must not be reported.
    func stop() {
        player?.delegate = nil
        player?.stop()
        player = nil
        if synthesizer.isSpeaking {
            _ = synthesizer.stopSpeaking(at: .immediate)
        }
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        guard player === self.player else { return }
        self.player = nil
        onFinished?()
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        guard player === self.player else { return }
        self.player = nil
        onFinished?()
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        onFinished?()
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        // Only ``stop()`` cancels, and that path deliberately reports nothing.
    }
}

// --------------------------------------------------------------------------- #
// Session
// --------------------------------------------------------------------------- #

/// The always-on "EVIE" loop: one WebSocket carries continuous microphone
/// audio up and conversation events down.
///
/// Protocol (see `backend/app/api/voice_live.py`): an `auth` text frame, then
/// binary PCM16 frames; the server answers with JSON events and expects a
/// `playback_finished` frame when reply audio stops — that frame is what opens
/// the follow-up window, so it is sent on every playback path, including the
/// failures.
@MainActor
final class HandsFreeSession: ObservableObject {
    /// Persisted so the loop comes back by itself after a restart.
    static let enabledDefaultsKey = "ev.handsFree.enabled"
    private static let deviceID = "mac-menubar"
    private static let maxBackoffSeconds = 30.0

    @Published var isEnabled: Bool {
        didSet {
            guard oldValue != isEnabled else { return }
            UserDefaults.standard.set(isEnabled, forKey: Self.enabledDefaultsKey)
            if isEnabled {
                start()
            } else {
                stop()
            }
        }
    }

    @Published var state: HandsFreeState = .off {
        didSet {
            guard oldValue != state else { return }
            onStateChange?(state)
        }
    }

    @Published var level: Double = 0
    @Published var caption = ""
    @Published var lastTranscript = ""
    @Published var lastReply = ""
    @Published var statusMessage = ""
    @Published var blockers: [String] = []

    /// Lets ``AppModel`` mirror the loop into the menu-bar glyph.
    var onStateChange: ((HandsFreeState) -> Void)?

    private let mic = HandsFreeMic()
    private let playback = HandsFreePlayback()

    private var baseURL: URL?
    private var token = ""

    private var socket: URLSessionWebSocketTask?
    private var connectTask: Task<Void, Never>?
    private var receiveTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var keepaliveTask: Task<Void, Never>?
    private var reconnectAttempt = 0
    private var audioConfig: (sampleRate: Double, frameSamples: Int)?
    private var configObserver: NSObjectProtocol?

    /// The stream outlives any single request, so the default 60 s timeouts
    /// would hang up between conversations.
    private lazy var urlSession: URLSession = {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 3_600
        configuration.timeoutIntervalForResource = 86_400
        return URLSession(configuration: configuration)
    }()

    init() {
        isEnabled = UserDefaults.standard.bool(forKey: Self.enabledDefaultsKey)
        playback.onFinished = { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                self.send(control: ["type": "playback_finished"])
            }
        }
    }

    /// Point the stream at the same API the rest of the app uses.
    func configure(baseURL: URL, token: String) {
        self.baseURL = baseURL
        self.token = token
    }

    /// Resume the loop on launch when the user left it switched on.
    func startIfEnabled() {
        guard isEnabled else { return }
        start()
    }

    // MARK: - Lifecycle

    private func start() {
        // Idempotent: a pending reconnect is already "on", so a second call
        // must not open a parallel stream.
        guard socket == nil, connectTask == nil, reconnectTask == nil else { return }
        statusMessage = ""
        blockers = []
        state = .connecting
        connectTask = Task { [weak self] in
            guard let self else { return }
            let granted = await self.requestMicrophoneAccess()
            self.connectTask = nil
            guard granted else {
                self.isEnabled = false
                self.state = .off
                return
            }
            self.connect()
        }
    }

    func stop() {
        connectTask?.cancel()
        connectTask = nil
        reconnectTask?.cancel()
        reconnectTask = nil
        reconnectAttempt = 0
        if socket != nil {
            send(control: ["type": "cancel"])
        }
        teardownConnection()
        audioConfig = nil
        state = .off
    }

    private func teardownConnection() {
        receiveTask?.cancel()
        receiveTask = nil
        keepaliveTask?.cancel()
        keepaliveTask = nil
        mic.stop()
        playback.stop()
        removeConfigObserver()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        level = 0
        caption = ""
    }

    // MARK: - Permissions

    /// Never fail silently: every refusal leaves a message the user can act on.
    private func requestMicrophoneAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            let granted = await AppForeground.withActivation {
                await AVCaptureDevice.requestAccess(for: .audio)
            }
            if !granted {
                statusMessage = "Microphone access was declined — hands-free needs it to hear “EVIE”."
            }
            return granted
        case .denied:
            statusMessage = "Microphone access is denied — open Permissions… and allow the microphone, then switch hands-free back on."
            return false
        case .restricted:
            statusMessage = "Microphone access is restricted by policy on this Mac; hands-free cannot listen."
            return false
        @unknown default:
            statusMessage = "Microphone access is in an unknown state; hands-free cannot listen."
            return false
        }
    }

    // MARK: - Transport

    private func liveURL() -> URL? {
        guard let baseURL, var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            return nil
        }
        switch components.scheme?.lowercased() {
        case "https", "wss":
            components.scheme = "wss"
        default:
            components.scheme = "ws"
        }
        let trimmed = components.path.hasSuffix("/")
            ? String(components.path.dropLast())
            : components.path
        components.path = trimmed + "/v1/voice/live"
        return components.url
    }

    private func connect() {
        guard let url = liveURL() else {
            statusMessage = "Hands-free needs a usable EV_API_URL; “\(baseURL?.absoluteString ?? "none")” is not one."
            isEnabled = false
            return
        }
        state = .connecting
        let task = urlSession.webSocketTask(with: url)
        socket = task
        task.resume()
        // Auth is the first frame rather than a query parameter so the bearer
        // token never lands in a proxy log.
        send(control: ["type": "auth", "token": token, "device_id": Self.deviceID])
        receiveTask = Task { [weak self] in
            await self?.receiveLoop(task)
        }
        startKeepalive()
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            do {
                let message = try await task.receive()
                guard task === socket else { return }
                switch message {
                case .string(let text):
                    handle(text: text)
                case .data(let data):
                    handle(text: String(decoding: data, as: UTF8.self))
                @unknown default:
                    break
                }
            } catch {
                guard task === socket else { return }
                handleDisconnect(reason: error.localizedDescription)
                return
            }
        }
    }

    /// App-level ping: the server answers `pong`, and a failed send is the
    /// earliest honest signal that a silent socket has died.
    private func startKeepalive() {
        keepaliveTask?.cancel()
        keepaliveTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                guard !Task.isCancelled, let self, self.socket != nil else { return }
                self.send(control: ["type": "ping"])
            }
        }
    }

    private func send(control frame: [String: Any]) {
        guard let socket,
              let data = try? JSONSerialization.data(withJSONObject: frame),
              let text = String(data: data, encoding: .utf8)
        else {
            return
        }
        socket.send(.string(text)) { [weak self] error in
            guard let error, let self else { return }
            Task { @MainActor in
                self.handleDisconnect(reason: error.localizedDescription)
            }
        }
    }

    private func handleDisconnect(reason: String) {
        // Both the receive loop and a failed send can land here; the first one
        // wins and the rest are no-ops.
        guard socket != nil else { return }
        teardownConnection()
        guard isEnabled else {
            state = .off
            return
        }
        let delay = min(Self.maxBackoffSeconds, pow(2.0, Double(reconnectAttempt)))
        reconnectAttempt += 1
        statusMessage = "Hands-free lost the connection (\(reason)); retrying in \(Int(delay))s."
        state = .connecting
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled, let self, self.isEnabled else { return }
            self.reconnectTask = nil
            self.connect()
        }
    }

    // MARK: - Events

    private func handle(text: String) {
        guard let data = text.data(using: .utf8),
              let raw = try? JSONSerialization.jsonObject(with: data),
              let event = raw as? [String: Any],
              let type = event["type"] as? String
        else {
            statusMessage = "Hands-free received an event it could not read."
            return
        }
        let payload = event["data"] as? [String: Any] ?? [:]

        switch type {
        case "ready":
            handleReady(payload)
        case "state":
            handleState(payload)
        case "level":
            level = payload["level"] as? Double ?? 0
        case "wake":
            if payload["stage"] as? String == "pending" {
                caption = "…"
            }
        case "partial":
            caption = payload["text"] as? String ?? ""
        case "transcript":
            lastTranscript = payload["text"] as? String ?? ""
            lastReply = ""
            caption = ""
        case "reply":
            let reply = payload["text"] as? String ?? ""
            lastReply = reply
            if payload["speak_locally"] as? Bool == true {
                speakLocally(reply)
            }
        case "audio":
            playReply(payload)
        case "barge_in":
            playback.stop()
        case "dismissed":
            caption = ""
            statusMessage = "EV ignored that (\(payload["reason"] as? String ?? "unknown"))."
        case "conversation_end":
            caption = ""
        case "error":
            let code = payload["code"] as? String ?? "error"
            let message = payload["message"] as? String ?? "hands-free failed"
            statusMessage = "\(code): \(message)"
        case "session", "pong":
            break
        default:
            break
        }
    }

    private func handleState(_ payload: [String: Any]) {
        guard let raw = payload["state"] as? String,
              let mapped = HandsFreeState(serverState: raw)
        else {
            return
        }
        state = mapped
        switch mapped {
        case .idle, .off:
            caption = ""
        case .waking, .listening:
            // A new turn supersedes whatever went wrong on the last one.
            statusMessage = ""
        default:
            break
        }
    }

    private func handleReady(_ payload: [String: Any]) {
        blockers = payload["blockers"] as? [String] ?? []
        guard payload["ready"] as? Bool == true else {
            // The server hangs up right after this; reconnecting would only
            // walk into the same wall, so report its verdict and switch off.
            statusMessage = blockers.isEmpty
                ? "The server is not ready for hands-free listening."
                : "EVIE cannot hear yet."
            isEnabled = false
            return
        }
        reconnectAttempt = 0
        statusMessage = ""
        let audio = payload["audio"] as? [String: Any] ?? [:]
        let sampleRate = audio["sample_rate"] as? Double ?? 16_000
        let frameMS = audio["frame_ms"] as? Double ?? 20
        let frameSamples = max(1, Int((sampleRate * frameMS / 1_000).rounded()))
        audioConfig = (sampleRate, frameSamples)
        startCapture()
    }

    // MARK: - Audio

    private func startCapture() {
        guard let audioConfig, let task = socket else { return }
        do {
            try mic.start(
                sampleRate: audioConfig.sampleRate,
                frameSamples: audioConfig.frameSamples
            ) { [weak task] frame in
                // Straight from the audio thread to the socket: a send failure
                // also breaks the receive loop, which owns the reconnect.
                task?.send(.data(frame)) { _ in }
            }
            observeConfigChanges()
            state = .idle
        } catch {
            statusMessage = error.localizedDescription
            isEnabled = false
        }
    }

    /// macOS stops the engine when the input hardware changes (headset plugged
    /// in, device switched); without this the tap goes quiet forever.
    private func observeConfigChanges() {
        removeConfigObserver()
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.restartCapture()
            }
        }
    }

    private func removeConfigObserver() {
        if let configObserver {
            NotificationCenter.default.removeObserver(configObserver)
        }
        configObserver = nil
    }

    private func restartCapture() {
        guard isEnabled, socket != nil, audioConfig != nil else { return }
        mic.stop()
        startCapture()
    }

    private func playReply(_ payload: [String: Any]) {
        guard let encoded = payload["audio_b64"] as? String,
              let audio = Data(base64Encoded: encoded),
              !audio.isEmpty
        else {
            statusMessage = "Reply audio was unreadable — EV stayed silent."
            send(control: ["type": "playback_finished"])
            return
        }
        do {
            try playback.play(data: audio)
        } catch {
            statusMessage = "Reply audio would not play: \(error.localizedDescription)"
            send(control: ["type": "playback_finished"])
        }
    }

    private func speakLocally(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            send(control: ["type": "playback_finished"])
            return
        }
        playback.speak(trimmed)
    }
}
