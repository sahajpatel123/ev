import AppKit
import EVAuth
import EVClient
import Foundation
import ServiceManagement

@MainActor
final class AppModel: ObservableObject {
    enum Status: String, Sendable {
        case offline
        case listening
        case thinking
        case speaking
    }

    struct ChatMessage: Identifiable, Equatable {
        let id: String
        let role: String
        var text: String
        var streaming: Bool
    }

    @Published var status: Status = .offline
    @Published var captureText = ""
    @Published var messages: [ChatMessage] = []
    @Published var hudCard: HUDCard?
    @Published var queueCount = 0
    @Published var lastError: String?
    @Published var isRecording = false
    @Published var sessionId: String?
    @Published var transcript = ""

    private(set) var config: AppConfig
    private(set) var client: EVAPIClient
    let queue: OfflineCaptureQueue
    let hotkey = GlobalHotkey()
    let mic = MicCapture()
    let player = TTSPlayer()

    private var heartbeatTask: Task<Void, Never>?
    private var conversationTask: Task<Void, Never>?
    private var recordLimitTask: Task<Void, Never>?
    private var pendingAssistantID: String?
    private var started = false
    private var sendingVoice = false

    init() {
        let config = AppConfig()
        self.config = config
        client = EVAPIClient(baseURL: config.baseURL, token: config.apiKey)
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("EV", isDirectory: true)
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("EV", isDirectory: true)
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        queue = OfflineCaptureQueue(store: FileCaptureQueueStore(directory: support))
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue {
                    try SMAppService.mainApp.register()
                } else {
                    try SMAppService.mainApp.unregister()
                }
            } catch {
                lastError = "Launch at login: \(error.localizedDescription)"
            }
        }
    }

    @discardableResult
    func reloadAPICredentials() -> Bool {
        if let stored = UserDefaults.standard.string(forKey: "EV_API_KEY"),
           !APIAuthKey.isUsable(stored) {
            UserDefaults.standard.removeObject(forKey: "EV_API_KEY")
        }
        let fresh = AppConfig()
        guard APIAuthKey.isUsable(fresh.apiKey) else { return false }
        let changed = fresh.apiKey != client.token || fresh.baseURL != client.baseURL
        config = fresh
        client = EVAPIClient(baseURL: fresh.baseURL, token: fresh.apiKey)
        return changed
    }

    private func isUnauthorized(_ error: Error) -> Bool {
        if case EVAPIError.httpStatus(401, _) = error { return true }
        return false
    }

    func start() {
        guard !started else { return }
        started = true
        _ = reloadAPICredentials()
        if config.usesPlaceholderKey {
            lastError = "API key is still the placeholder “dev”. EV.app now reads EV_MASTER_KEY from ~/Library/Application Support/EV/api.env, ~/.ev/env, or the repo .env. Rebuild the app after packaging so Talk and chat authenticate."
        }
        Task {
            await refresh()
        }
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard !Task.isCancelled else { break }
                await self?.tick()
            }
        }
        conversationTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                guard !Task.isCancelled else { break }
                await self?.refreshConversation()
            }
        }
    }

    func tick() async {
        await refreshHealth()
        await syncQueue()
        await updateQueueCount()
        await refreshConversation()
    }

    func refresh() async {
        await refreshHealth()
        await refreshHUD()
        await syncQueue()
        await updateQueueCount()
        await refreshConversation()
    }

    func refreshHealth() async {
        do {
            _ = try await client.health()
            if status == .offline {
                status = .listening
            }
            let listener = RuntimeListener(client: client)
            let listenerState = status == .offline ? "off" : "listening"
            _ = try? await listener.heartbeat(deviceID: config.deviceID, listenerState: listenerState)
        } catch {
            if isUnauthorized(error), reloadAPICredentials() {
                await refreshHealth()
                return
            }
            status = .offline
        }
    }

    func refreshHUD() async {
        hudCard = try? await client.hudCard()
    }

    func refreshConversation() async {
        guard pendingAssistantID == nil, !isRecording, !sendingVoice,
              status != .thinking, status != .speaking else { return }
        do {
            let detail = try await client.conversation(limit: 40)
            let mapped = detail.messages.map { message in
                ChatMessage(id: message.id, role: message.role, text: message.text, streaming: false)
            }
            if mapped != messages {
                messages = mapped
            }
            if lastError?.contains("401") == true || lastError?.contains("placeholder") == true {
                lastError = nil
            }
        } catch {
            if isUnauthorized(error), reloadAPICredentials() {
                await refreshConversation()
                return
            }
            if case EVAPIError.httpStatus(401, let body) = error {
                lastError = authFailureMessage(body)
            }
        }
    }

    func syncQueue() async {
        let summary = await queue.sync(using: client)
        if !summary.errors.isEmpty {
            lastError = summary.errors.first
        }
        await updateQueueCount()
    }

    func updateQueueCount() async {
        queueCount = (try? queue.pending().count) ?? 0
    }

    // MARK: - Capture

    func capture() {
        let text = captureText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        captureText = ""
        let payload = CapturePayload(
            source: "mac",
            eventType: "note",
            text: text,
            deviceID: config.deviceID
        )
        Task {
            do {
                let result = try await client.capture(payload: payload)
                lastError = result.duplicate ? "Duplicate capture — already stored." : nil
            } catch EVAPIError.transport {
                do {
                    _ = try queue.enqueue(payload)
                    lastError = "Offline — capture queued for sync."
                } catch {
                    lastError = "Queue write failed: \(error)"
                }
            } catch {
                lastError = formattedAPIError(error, fallback: "Capture failed")
            }
            await updateQueueCount()
        }
    }

    // MARK: - Streaming chat

    func sendChat(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        messages.append(ChatMessage(id: UUID().uuidString, role: "user", text: trimmed, streaming: false))
        let id = UUID().uuidString
        pendingAssistantID = id
        messages.append(ChatMessage(id: id, role: "assistant", text: "", streaming: true))
        status = .thinking

        Task {
            do {
                for try await event in client.askStream(
                    trimmed,
                    deviceId: config.deviceID
                ) {
                    switch event {
                    case .status:
                        status = .thinking
                    case .delta(let chunk, _):
                        if let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].text += chunk
                        }
                    case .refined(let text):
                        if let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].text = text
                        }
                    case .error(let message):
                        lastError = message
                    case .done:
                        if let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].streaming = false
                        }
                        status = .listening
                    default:
                        break
                    }
                }
            } catch {
                lastError = formattedAPIError(error, fallback: "Chat failed")
                if let index = messages.firstIndex(where: { $0.id == id }) {
                    messages[index].streaming = false
                }
                status = .listening
            }
            pendingAssistantID = nil
        }
    }

    // MARK: - Voice (push-to-talk fallback — always-on wake is ev.ears)

    func toggleTalk() {
        if isRecording {
            stopAndSend()
        } else if sendingVoice {
            return
        } else {
            startRecording()
        }
    }

    func startRecording() {
        Task {
            let granted = await mic.start()
            isRecording = granted
            status = granted ? .listening : status
            if !granted {
                lastError = "Microphone permission denied — open EV → Permissions for the fix."
                return
            }
            lastError = nil
            recordLimitTask?.cancel()
            recordLimitTask = Task { [weak self] in
                let nanos = UInt64(MicCapture.maxSeconds * 1_000_000_000)
                try? await Task.sleep(nanoseconds: nanos)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self?.stopAndSend()
                }
            }
        }
    }

    func stopAndSend() {
        recordLimitTask?.cancel()
        recordLimitTask = nil
        Task {
            let data = mic.stop()
            isRecording = false
            guard let data, !data.isEmpty else {
                if !sendingVoice {
                    status = .listening
                    lastError = "I didn’t hear anything. Hold Push to talk and try again."
                }
                return
            }
            if MicCapture.isQuiet(data) {
                status = .listening
                lastError = "That clip was too quiet. Hold Push to talk closer to the mic."
                return
            }
            let audioB64 = data.base64EncodedString()
            sendingVoice = true
            status = .thinking
            lastError = nil
            defer { sendingVoice = false }
            do {
                // Reuse an already-open Talk session. A second press must not
                // end the follow-up and start a new wake cycle.
                guard let session = try await openTalkSession(audioB64: audioB64) else {
                    lastError = "No voice session — grant voice consent in Permissions."
                    status = .listening
                    return
                }
                var streamedAudio = false
                var assistantID: String?
                for try await event in client.streamUtterance(
                    sessionId: session,
                    audioB64: audioB64,
                    pushToTalk: true
                ) {
                    switch event {
                    case .partial:
                        break
                    case .transcript(let spoken):
                        transcript = spoken.text
                        if !spoken.text.isEmpty {
                            messages.append(ChatMessage(id: UUID().uuidString, role: "user", text: spoken.text, streaming: false))
                        }
                        let id = UUID().uuidString
                        assistantID = id
                        messages.append(ChatMessage(id: id, role: "assistant", text: "", streaming: true))
                    case .ttsChunk(let chunk):
                        // One speaker: play TTS bytes only. Never /usr/bin/say
                        // alongside real audio, and never say() a text chunk
                        // that later arrives as TTS.
                        if let b64 = chunk.audioB64, let data = Data(base64Encoded: b64), !data.isEmpty {
                            streamedAudio = true
                            status = .speaking
                            try? player.enqueue(data)
                        }
                    case .reply(let response):
                        if let id = assistantID, let index = messages.firstIndex(where: { $0.id == id }) {
                            messages[index].text = response.reply
                            messages[index].streaming = false
                        } else {
                            messages.append(ChatMessage(id: UUID().uuidString, role: "assistant", text: response.reply, streaming: false))
                        }
                        if response.state == "ended" {
                            sessionId = nil
                        }
                        if let err = response.error, !err.isEmpty {
                            lastError = err
                        }
                        if !streamedAudio {
                            await playReply(response)
                        }
                    case .error(let message):
                        lastError = message
                    case .done:
                        break
                    }
                }
                if let id = assistantID, let index = messages.firstIndex(where: { $0.id == id }) {
                    messages[index].streaming = false
                }
                status = streamedAudio && player.isPlaying ? .speaking : .listening
            } catch {
                sessionId = nil
                lastError = formattedAPIError(error, fallback: "Voice failed")
                status = .listening
            }
        }
    }

    private func openTalkSession(audioB64: String) async throws -> String? {
        if let existing = sessionId {
            return existing
        }
        // Wake is session setup only — do not upload the spoken clip
        // twice (that doubled Whisper work and blew the 60s timeout).
        let wake = try await client.wakeVoice(
            deviceId: config.deviceID,
            pushToTalk: true
        )
        sessionId = wake.sessionId
        guard let session = wake.sessionId else {
            return nil
        }
        if wake.state == "verifying", wake.ownerEnrolled, let nonce = wake.challengeNonce {
            let verify = try await client.verifyVoice(
                sessionId: session,
                nonce: nonce,
                phrase: wake.challengePhrase,
                samples: [audioB64]
            )
            if !verify.verified {
                lastError = "Speaker verification failed: \(verify.reason)"
                sessionId = nil
                return nil
            }
        }
        return session
    }

    func playReply(_ response: VoiceUtteranceResponse) async {
        status = .speaking
        // decide_playback: TTS audio/ref → play once; never /usr/bin/say
        // as a parallel voice. No audio → stay silent (overlay/text only).
        if let b64 = response.tts?.audioB64, let data = Data(base64Encoded: b64), !data.isEmpty {
            do {
                try player.play(data: data)
                return
            } catch {
                lastError = "TTS playback failed: \(error.localizedDescription)"
            }
        }
        if let ref = response.tts?.audioRef, !ref.isEmpty {
            do {
                let data = try await client.voiceAudio(ref: ref)
                try player.play(data: data)
                return
            } catch {
                lastError = "TTS playback failed: \(error.localizedDescription)"
            }
        }
    }

    func playAudio(ref: String) async {
        do {
            let data = try await client.voiceAudio(ref: ref)
            try player.play(data: data)
        } catch {
            lastError = "TTS playback failed: \(error.localizedDescription)"
        }
    }

    private func speakFallback(_ text: String) {
        let spoken = String(text.prefix(280))
        guard !spoken.isEmpty else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        proc.arguments = ["-v", "Ava", "-r", "160", spoken]
        try? proc.run()
    }

    private func formattedAPIError(_ error: Error, fallback: String) -> String {
        if let apiError = error as? EVAPIError {
            switch apiError {
            case .httpStatus(401, let body):
                return authFailureMessage(body)
            case .httpStatus(let code, let body):
                let detail = body.trimmingCharacters(in: .whitespacesAndNewlines)
                if detail.isEmpty {
                    return "\(fallback): API error \(code)."
                }
                return "\(fallback): \(detail.prefix(240))"
            case .transport(let message) where message.lowercased().contains("timed out"):
                return "\(fallback): the reply took too long. Hold Push to talk and ask again — keep it under \(Int(MicCapture.maxSeconds)) seconds."
            case .transport(let message):
                return "\(fallback): network error — \(message)"
            case .decoding(let message):
                return "\(fallback): bad reply — \(message)"
            }
        }
        return "\(fallback): \(error.localizedDescription)"
    }

    private func authFailureMessage(_ body: String) -> String {
        let detail = body.isEmpty ? "invalid or revoked device token" : body
        return "API rejected this Mac’s key (\(detail)). EV.app must use the same EV_MASTER_KEY as the API — package.sh writes it to ~/Library/Application Support/EV/api.env."
    }
}
