import Foundation
#if os(iOS) || os(macOS)
import AVFoundation
#endif

/// One event from the full-duplex `WS /v1/voice/live` conversation channel.
public struct LiveVoiceEvent: Sendable {
    public let type: String
    public let text: String?
    public let audioB64: String?
    public let audioRef: String?
    public let state: [String: String]
    public let sessionId: String?
    public let conversationId: String?
    public let code: String?
    public let fatal: Bool
    public let metric: String?
    public let ms: Int?

    public init(
        type: String,
        text: String? = nil,
        audioB64: String? = nil,
        audioRef: String? = nil,
        state: [String: String] = [:],
        sessionId: String? = nil,
        conversationId: String? = nil,
        code: String? = nil,
        fatal: Bool = false,
        metric: String? = nil,
        ms: Int? = nil
    ) {
        self.type = type
        self.text = text
        self.audioB64 = audioB64
        self.audioRef = audioRef
        self.state = state
        self.sessionId = sessionId
        self.conversationId = conversationId
        self.code = code
        self.fatal = fatal
        self.metric = metric
        self.ms = ms
    }
}

/// A client-side WebSocket for EV LIVE.
///
/// Audio is sent as raw 16 kHz mono PCM16 frames while the server sends state,
/// partial transcripts, backchannels, barge-in signals, and playable TTS
/// chunks on the same connection.
public final class LiveVoiceConnection: @unchecked Sendable {
    private let baseURL: URL
    private let token: String
    private let session: URLSession
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var streamContinuation: AsyncThrowingStream<LiveVoiceEvent, Error>.Continuation?
    private var sendTail: Task<Void, Never>?
    private let lock = NSLock()

    public init(
        baseURL: URL,
        token: String,
        session: URLSession = EVAPIClient.voiceSession
    ) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    public func connect(
        sessionId: String
    ) async throws -> AsyncThrowingStream<LiveVoiceEvent, Error> {
        close()
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw EVAPIError.transport("invalid EV LIVE base URL")
        }
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.path = components.path
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            + "/v1/voice/live"
        components.queryItems = [
            URLQueryItem(name: "session_id", value: sessionId),
            URLQueryItem(name: "token", value: token),
        ]
        guard let url = components.url else {
            throw EVAPIError.transport("invalid EV LIVE WebSocket URL")
        }

        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let task = session.webSocketTask(with: request)
        socket = task

        let stream = AsyncThrowingStream<LiveVoiceEvent, Error> { continuation in
            self.streamContinuation = continuation
            continuation.onTermination = { [weak self] _ in
                self?.close()
            }
        }
        task.resume()
        receiveTask = Task { [weak self] in
            await self?.receiveLoop(task)
        }
        return stream
    }

    /// Queue a PCM frame behind earlier frames so microphone callbacks never
    /// race each other on the URLSession WebSocket task.
    public func enqueuePCM(_ data: Data) {
        lock.lock()
        let previous = sendTail
        let task = socket
        sendTail = Task {
            await previous?.value
            guard let task else { return }
            try? await task.send(.data(data))
        }
        lock.unlock()
    }

    public func sendControl(_ action: String) {
        sendJSON(["type": "control", "action": action])
    }

    public func sendPlayback(active: Bool) {
        sendJSON(["type": "playback", "active": active])
    }

    public func sendText(_ text: String, commit: Bool = true) {
        sendJSON(["type": "text", "text": text, "commit": commit])
    }

    public func close() {
        receiveTask?.cancel()
        receiveTask = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        streamContinuation?.finish()
        streamContinuation = nil
        lock.lock()
        sendTail?.cancel()
        sendTail = nil
        lock.unlock()
    }

    private func sendJSON(_ object: [String: Any]) {
        guard let task = socket, JSONSerialization.isValidJSONObject(object) else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        lock.lock()
        let previous = sendTail
        sendTail = Task {
            await previous?.value
            try? await task.send(.string(String(decoding: data, as: UTF8.self)))
        }
        lock.unlock()
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) async {
        do {
            while !Task.isCancelled {
                let message = try await task.receive()
                let data: Data
                switch message {
                case .data(let value):
                    data = value
                case .string(let value):
                    data = Data(value.utf8)
                @unknown default:
                    continue
                }
                if let event = decode(data) {
                    streamContinuation?.yield(event)
                    if event.fatal { break }
                }
            }
            streamContinuation?.finish()
        } catch is CancellationError {
            streamContinuation?.finish()
        } catch {
            streamContinuation?.finish(throwing: error)
        }
    }

    private func decode(_ data: Data) -> LiveVoiceEvent? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = object["type"] as? String
        else { return nil }
        let state = (object["state"] as? [String: Any] ?? [:]).reduce(into: [String: String]()) {
            if let value = $1.value as? String { $0[$1.key] = value }
        }
        let msValue: Int?
        if let number = object["ms"] as? Int {
            msValue = number
        } else if let number = object["ms"] as? Double {
            msValue = Int(number)
        } else {
            msValue = nil
        }
        return LiveVoiceEvent(
            type: type,
            text: object["text"] as? String,
            audioB64: object["audio_b64"] as? String,
            audioRef: object["audio_ref"] as? String,
            state: state,
            sessionId: object["session_id"] as? String,
            conversationId: object["conversation_id"] as? String,
            code: object["code"] as? String,
            fatal: object["fatal"] as? Bool ?? false,
            metric: object["metric"] as? String,
            ms: msValue
        )
    }
}

#if os(iOS) || os(macOS)
/// Captures microphone audio in the format consumed by EV LIVE.
///
/// The audio engine may run at the device's native rate. Conversion happens
/// before frames enter the WebSocket so the server receives a stable 16 kHz,
/// mono, PCM16 stream regardless of hardware.
public final class LiveVoiceMicrophone: @unchecked Sendable {
    public let sampleRate: Double = 16_000

    private var engine = AVAudioEngine()
    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var running = false
    private var tapInstalled = false

    public init() {}

    /// Starts capture and forwards each converted frame to the live socket.
    public func start(enqueue: @escaping @Sendable (Data) -> Void) throws {
        stop()

        #if os(iOS)
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.record, mode: .measurement, options: [.allowBluetooth])
        try audioSession.setActive(true)
        #endif

        // Recreate after a just-accepted grant. An engine allocated before
        // Allow reports 0 Hz / 0 ch; installTap on that format aborts.
        engine = AVAudioEngine()
        let inputFormat: AVAudioFormat
        do {
            inputFormat = try AVAudioSafe.attachAndPrepare(engine)
        } catch {
            throw EVAPIError.transport(error.localizedDescription)
        }
        let input = engine.inputNode
        // Do not enable AVAudio voice processing here. On a menu-bar
        // accessory it aborts the process when playback starts on a
        // second audio unit (Talk / TTS), which looked like the app quit.
        // Do not fall back to outputFormat for the tap — a mismatched
        // tap format also aborts.
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw EVAPIError.transport("microphone input format is unavailable")
        }
        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw EVAPIError.transport("unable to create EV LIVE audio format")
        }
        guard let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            throw EVAPIError.transport("unable to create EV LIVE audio converter")
        }
        self.converter = converter

        let ratio = sampleRate / max(inputFormat.sampleRate, 1)
        do {
            try AVAudioSafe.installTap(on: input, bufferSize: 512, format: inputFormat) { [weak self] buffer, _ in
                guard let self else { return }
                self.lock.lock()
                defer { self.lock.unlock() }
                guard self.running, let converter = self.converter else { return }
                let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1
                guard let converted = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
                    return
                }
                var conversionError: NSError?
                let status = converter.convert(to: converted, error: &conversionError) { _, outStatus in
                    outStatus.pointee = .haveData
                    return buffer
                }
                guard status == .haveData, converted.frameLength > 0,
                      let channel = converted.int16ChannelData?.pointee else {
                    return
                }
                let data = Data(bytes: channel, count: Int(converted.frameLength) * MemoryLayout<Int16>.size)
                enqueue(data)
            }
        } catch {
            self.converter = nil
            throw EVAPIError.transport(error.localizedDescription)
        }
        tapInstalled = true

        do {
            lock.lock()
            running = true
            lock.unlock()
            try AVAudioSafe.start(engine)
        } catch {
            lock.lock()
            running = false
            lock.unlock()
            removeTapIfNeeded()
            self.converter = nil
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setActive(false)
            #endif
            throw error
        }
    }

    public func stop() {
        lock.lock()
        let wasRunning = running
        running = false
        lock.unlock()
        guard wasRunning || converter != nil || tapInstalled else { return }
        removeTapIfNeeded()
        AVAudioSafe.stop(engine)
        converter = nil
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    private func removeTapIfNeeded() {
        guard tapInstalled else { return }
        AVAudioSafe.removeTap(on: engine.inputNode)
        tapInstalled = false
    }

    deinit {
        stop()
    }
}
#endif
