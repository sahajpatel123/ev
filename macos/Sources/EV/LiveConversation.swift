import EVClient
import EVRuntime
import Foundation

/// Full-duplex conversation that starts when EV.app opens.
///
/// Opening the app is the door: this opens `POST /v1/voice/live/open`,
/// streams 16 kHz PCM into `WS /v1/voice/live`, plays streamed TTS, and
/// stops playback on barge-in. No wake word is required.
@MainActor
final class LiveConversation {
    private(set) var isActive = false
    private(set) var isMuted = false
    /// True while the reconnect loop is scheduled (including between sockets).
    var isRunning: Bool { loopTask != nil }

    private weak var model: AppModel?
    private var connection: LiveVoiceConnection?
    private let microphone = LiveVoiceMicrophone()
    private var loopTask: Task<Void, Never>?
    private var assistantID: String?
    private var stayMuted = false

    func attach(_ model: AppModel) {
        self.model = model
        model.player.onPlayingChange = { [weak self] playing in
            Task { @MainActor in
                guard let self else { return }
                self.connection?.sendPlayback(active: playing)
                guard let model = self.model else { return }
                if playing {
                    model.status = .speaking
                } else if model.status == .speaking {
                    model.status = .listening
                }
            }
        }
    }

    func start() {
        guard loopTask == nil else { return }
        stayMuted = false
        isMuted = false
        EarsProcess.stopAndWait()
        loopTask = Task { [weak self] in
            await self?.runLoop()
        }
    }

    func stop() {
        loopTask?.cancel()
        loopTask = nil
        tearDownChannel()
        isActive = false
        model?.isLiveActive = false
    }

    func toggleMute() {
        if isMuted {
            isMuted = false
            stayMuted = false
            model?.isLiveMuted = false
            model?.lastError = nil
            if let connection, isActive {
                startMicrophone(on: connection)
            }
            return
        }
        isMuted = true
        stayMuted = true
        model?.isLiveMuted = true
        microphone.stop()
        model?.player.stop()
        connection?.sendControl("quiet")
        model?.status = .listening
    }

    private func runLoop() async {
        defer {
            loopTask = nil
            isActive = false
            model?.isLiveActive = false
        }
        guard let model else { return }
        let granted = await MicrophoneAuthorization.requestAccess()
        if !granted {
            model.noteMicrophoneDenied()
            return
        }
        if model.config.usesPlaceholderKey {
            model.lastError = "API key is still the placeholder. Live listen cannot start."
            if model.status == .offline {
                await model.refreshHealth()
            }
            return
        }
        while !Task.isCancelled {
            if stayMuted {
                try? await Task.sleep(nanoseconds: 400_000_000)
                continue
            }
            do {
                try await connectOnce()
            } catch is CancellationError {
                return
            } catch {
                if Task.isCancelled { return }
                model.lastError = modelFormatted(error)
                model.status = .offline
                isActive = false
                model.isLiveActive = false
                tearDownChannel()
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    private func connectOnce() async throws {
        guard let model else { return }
        let opened = try await model.client.openLiveVoice(deviceId: model.config.deviceID)
        model.sessionId = opened.sessionId
        let connection = LiveVoiceConnection(
            baseURL: model.client.baseURL,
            token: model.client.token
        )
        self.connection = connection
        let stream = try await connection.connect(sessionId: opened.sessionId)
        startMicrophone(on: connection)
        isActive = true
        model.isLiveActive = true
        model.isLiveMuted = false
        model.status = .listening
        model.lastError = nil

        do {
            for try await event in stream {
                if Task.isCancelled { break }
                await handle(event)
                if event.fatal {
                    // Fatal is a channel close, never a process quit.
                    if event.code == "listening_stopped" {
                        stayMuted = true
                        isMuted = true
                        model.isLiveMuted = true
                        microphone.stop()
                        model.player.stop()
                    }
                    break
                }
            }
        } catch is CancellationError {
            tearDownChannel()
            throw CancellationError()
        }
        tearDownChannel()
        isActive = false
        model.isLiveActive = false
        if !stayMuted, !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 400_000_000)
        }
    }

    private func startMicrophone(on connection: LiveVoiceConnection) {
        microphone.stop()
        guard AudioInputLease.acquire(.live) else {
            model?.noteMicrophoneCaptureFailed("already in use")
            return
        }
        do {
            try microphone.start { [weak connection] data in
                connection?.enqueuePCM(data)
            }
            isMuted = false
            model?.isLiveMuted = false
        } catch {
            // Keep the live lease so Talk cannot start a second engine.
            model?.noteMicrophoneCaptureFailed(error.localizedDescription)
        }
    }

    private func handle(_ event: LiveVoiceEvent) async {
        guard let model else { return }
        switch event.type {
        case "ready":
            model.status = .listening
        case "state":
            apply(phase: event.state["phase"])
        case "partial":
            if let text = event.text, !text.isEmpty {
                model.transcript = text
            }
        case "final_transcript":
            if let text = event.text, !text.isEmpty {
                model.transcript = text
                model.messages.append(
                    AppModel.ChatMessage(id: UUID().uuidString, role: "user", text: text, streaming: false)
                )
                let id = UUID().uuidString
                assistantID = id
                model.messages.append(
                    AppModel.ChatMessage(id: id, role: "assistant", text: "", streaming: true)
                )
                model.status = .thinking
            }
        case "backchannel":
            await playAudio(event)
        case "tts_chunk":
            model.status = .speaking
            if let text = event.text, !text.isEmpty, let id = assistantID,
               let index = model.messages.firstIndex(where: { $0.id == id }),
               model.messages[index].text.isEmpty {
                model.messages[index].text = text
            }
            await playAudio(event)
        case "reply":
            if let text = event.text {
                if let id = assistantID, let index = model.messages.firstIndex(where: { $0.id == id }) {
                    model.messages[index].text = text
                    model.messages[index].streaming = false
                } else if !text.isEmpty {
                    model.messages.append(
                        AppModel.ChatMessage(id: UUID().uuidString, role: "assistant", text: text, streaming: false)
                    )
                }
            }
            assistantID = nil
            if !model.player.isPlaying {
                model.status = .listening
            }
        case "barge_in":
            model.player.stop()
            connection?.sendPlayback(active: false)
            model.status = .listening
        case "error":
            if event.fatal {
                if let message = event.text, !message.isEmpty {
                    model.lastError = message
                } else if let code = event.code {
                    model.lastError = code
                }
            }
        default:
            break
        }
    }

    private func apply(phase: String?) {
        guard let model, let phase else { return }
        switch phase {
        case "thinking", "reasoning":
            model.status = .thinking
        case "speaking", "speaking_and_listening":
            model.status = model.player.isPlaying ? .speaking : .listening
        case "interrupted":
            model.status = .listening
        default:
            if model.status != .speaking || !model.player.isPlaying {
                model.status = .listening
            }
        }
    }

    private func playAudio(_ event: LiveVoiceEvent) async {
        guard let model else { return }
        if let b64 = event.audioB64, let data = Data(base64Encoded: b64), !data.isEmpty {
            try? model.player.enqueue(data)
            return
        }
        if let ref = event.audioRef, !ref.isEmpty {
            do {
                let data = try await model.client.voiceAudio(ref: ref)
                try model.player.enqueue(data)
            } catch {
                model.lastError = "TTS playback failed: \(error.localizedDescription)"
            }
        }
    }

    private func tearDownChannel() {
        microphone.stop()
        AudioInputLease.release(.live)
        connection?.close()
        connection = nil
    }

    private func modelFormatted(_ error: Error) -> String {
        model?.formattedLiveError(error) ?? error.localizedDescription
    }
}
