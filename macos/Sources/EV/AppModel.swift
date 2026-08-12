import AppKit
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
        let id = UUID()
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

    let config = AppConfig()
    let client: EVAPIClient
    let queue: OfflineCaptureQueue
    let hotkey = GlobalHotkey()
    let mic = MicCapture()
    let player = TTSPlayer()

    private var heartbeatTask: Task<Void, Never>?
    private var pendingAssistantID: UUID?

    init() {
        let config = AppConfig()
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

    func start() {
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
    }

    func tick() async {
        await refreshHealth()
        await syncQueue()
        await updateQueueCount()
    }

    func refresh() async {
        await refreshHealth()
        await refreshHUD()
        await syncQueue()
        await updateQueueCount()
    }

    func refreshHealth() async {
        do {
            _ = try await client.health()
            if status == .offline {
                status = .listening
            }
            let listener = RuntimeListener(client: client)
            _ = try? await listener.heartbeat(deviceID: config.deviceID, listenerState: status.rawValue)
        } catch {
            status = .offline
        }
    }

    func refreshHUD() async {
        hudCard = try? await client.hudCard()
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
                lastError = "Capture failed: \(error)"
            }
            await updateQueueCount()
        }
    }

    // MARK: - Streaming chat

    func sendChat(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        messages.append(ChatMessage(role: "user", text: trimmed, streaming: false))
        let id = UUID()
        pendingAssistantID = id
        messages.append(ChatMessage(role: "assistant", text: "", streaming: true))
        status = .thinking

        Task {
            do {
                for try await event in client.askStream(
                    trimmed,
                    deviceId: config.deviceID
                ) {
                    switch event {
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
                lastError = "Chat failed: \(error.localizedDescription)"
                if let index = messages.firstIndex(where: { $0.id == id }) {
                    messages[index].streaming = false
                }
                status = .listening
            }
        }
    }

    // MARK: - Voice

    func toggleTalk() {
        if isRecording {
            stopAndSend()
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
            }
        }
    }

    func stopAndSend() {
        Task {
            let data = mic.stop()
            isRecording = false
            guard let data, !data.isEmpty else {
                status = .listening
                return
            }
            let audioB64 = data.base64EncodedString()
            status = .thinking
            do {
                let wake = try await client.wakeVoice(deviceId: config.deviceID)
                sessionId = wake.sessionId
                guard let session = wake.sessionId else {
                    lastError = wake.message ?? "No voice session — enroll a voiceprint first."
                    status = .listening
                    return
                }
                if wake.ownerEnrolled, let nonce = wake.challengeNonce {
                    let verify = try await client.verifyVoice(
                        sessionId: session,
                        nonce: nonce,
                        phrase: wake.challengePhrase,
                        samples: [audioB64]
                    )
                    if !verify.verified {
                        lastError = "Speaker verification failed: \(verify.reason)"
                        status = .listening
                        return
                    }
                }
                let response = try await client.utterance(
                    sessionId: session,
                    audioB64: audioB64
                )
                transcript = response.transcript
                messages.append(ChatMessage(role: "user", text: response.transcript, streaming: false))
                messages.append(ChatMessage(role: "assistant", text: response.reply, streaming: false))
                if let audioRef = response.tts?.audioRef {
                    status = .speaking
                    await playAudio(ref: audioRef)
                }
                status = .listening
            } catch {
                lastError = "Voice failed: \(error.localizedDescription)"
                status = .listening
            }
        }
    }

    func playAudio(ref: String) async {
        do {
            let url: URL
            if let absolute = URL(string: ref), absolute.scheme != nil {
                url = absolute
            } else {
                url = config.baseURL.appendingPathComponent(ref.hasPrefix("/") ? String(ref.dropFirst()) : ref)
            }
            var request = URLRequest(url: url)
            request.setValue("Bearer \(config.apiKey)", forHTTPHeaderField: "Authorization")
            let (data, _) = try await client.session.data(for: request)
            try player.play(data: data)
        } catch {
            lastError = "TTS playback failed: \(error.localizedDescription)"
        }
    }
}
