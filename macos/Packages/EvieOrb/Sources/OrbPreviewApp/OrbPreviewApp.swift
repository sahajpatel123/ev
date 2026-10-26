import AppKit
import AVFoundation
import EvieOrb
import SwiftUI

// Standalone owner live-check app (R8). The owner runs it later via
// `macos/scripts/orb-preview`. NEVER launched by automation (O3/O4):
// it opens windows and can use the microphone (only after the owner
// clicks "Use microphone").

@main
struct OrbPreviewAppMain: App {
    var body: some Scene {
        WindowGroup("Evie Orb — Live Check") {
            OrbPreviewRoot()
                .frame(minWidth: 560, minHeight: 640)
        }
    }
}

final class PreviewModel: ObservableObject {
    @Published var state: OrbState = .idle
    @Published var inputLevel: Double = 0
    @Published var outputLevel: Double = 0
    @Published var useMic = false
    @Published var micDenied = false
    @Published var cpuPct: Double = 0
    @Published var fps: Double = 0
    @Published var gpuMs: Double = -1
    @Published var darkBackdrop = true

    let manual = ManualLevelSource()
    let host = IslandHost()
    private let engine = AVAudioEngine()
    private var lastCPU = PreviewModel.cpuTime()
    private var lastWall = Date()
    private var lastFrames: UInt64 = 0
    private var timer: Timer?

    static func cpuTime() -> Double {
        var u = rusage()
        getrusage(RUSAGE_SELF, &u)
        return Double(u.ru_utime.tv_sec) + Double(u.ru_utime.tv_usec) / 1_000_000
            + Double(u.ru_stime.tv_sec) + Double(u.ru_stime.tv_usec) / 1_000_000
    }

    func start() {
        host.orbView.levelSource = manual
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.sample()
        }
    }

    func stop() {
        timer?.invalidate()
        stopMic()
    }

    var activeSource: OrbLevelSource { useMic ? AtomicOrbLevelMeter.shared : manual }

    func applyState() {
        host.orbView.state = state
        host.updateLayout(animated: false)
    }

    func applyLevels() {
        manual.setLevels(input: Float(inputLevel), output: Float(outputLevel))
    }

    func startMic() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            beginMicEngine()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                DispatchQueue.main.async {
                    if granted {
                        self?.beginMicEngine()
                    } else {
                        self?.micDenied = true
                    }
                }
            }
        default:
            micDenied = true
        }
    }

    func stopMic() {
        if engine.isRunning {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        useMic = false
        host.orbView.levelSource = manual
    }

    private func beginMicEngine() {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { return }
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { buffer, _ in
            guard let floatData = buffer.floatChannelData else { return }
            let frames = Int(buffer.frameLength)
            var pcm = Data(capacity: frames * 2)
            for i in 0..<frames {
                let clamped = min(1, max(-1, floatData[0][i]))
                var s = Int16(clamped * 32767)
                withUnsafeBytes(of: &s) { pcm.append(contentsOf: $0) }
            }
            AtomicOrbLevelMeter.shared.ingestInputPCM16(pcm)
        }
        do {
            try engine.start()
        } catch {
            return
        }
        useMic = true
        host.orbView.levelSource = AtomicOrbLevelMeter.shared
    }

    private func sample() {
        let now = Date()
        let cpu = Self.cpuTime()
        let wall = now.timeIntervalSince(lastWall)
        if wall > 0 {
            cpuPct = 100 * (cpu - lastCPU) / wall
        }
        lastCPU = cpu
        lastWall = now
        let frames = host.orbView.framesRendered
        if wall > 0 {
            fps = Double(frames - lastFrames) / wall
        }
        lastFrames = frames
        gpuMs = host.orbView.gpuTimeMilliseconds
        print(String(
            format: "[orb-preview] cpu=%.2f%% fps=%.1f gpu=%.3fms state=%@",
            cpuPct, fps, gpuMs, state.rawValue
        ))
    }
}

struct OrbPreviewRoot: View {
    @StateObject private var model = PreviewModel()

    var body: some View {
        VStack(spacing: 12) {
            ZStack {
                (model.darkBackdrop ? Color(white: 0.07) : Color(white: 0.93))
                    .cornerRadius(12)
                OrbView(
                    state: model.state,
                    levelSource: model.activeSource,
                    appearance: model.darkBackdrop ? .dark : .light
                )
                .frame(width: 220, height: 220)
            }
            .frame(height: 260)

            Picker("State", selection: $model.state) {
                ForEach(OrbState.allCases, id: \.self) { s in
                    Text(s.rawValue).tag(s)
                }
            }
            .pickerStyle(.segmented)
            .onChange(of: model.state) { _, _ in model.applyState() }

            HStack {
                Text("Input")
                Slider(value: $model.inputLevel, in: 0...1)
                    .disabled(model.useMic)
                    .onChange(of: model.inputLevel) { _, _ in model.applyLevels() }
                Text(String(format: "%.2f", model.useMic ? Double(AtomicOrbLevelMeter.shared.inputLevel) : model.inputLevel))
                    .monospacedDigit()
                    .frame(width: 40)
            }
            HStack {
                Text("Output")
                Slider(value: $model.outputLevel, in: 0...1)
                    .onChange(of: model.outputLevel) { _, _ in model.applyLevels() }
                Text(String(format: "%.2f", model.outputLevel))
                    .monospacedDigit()
                    .frame(width: 40)
            }

            HStack {
                if model.useMic {
                    Button("Stop microphone") { model.stopMic() }
                } else {
                    Button("Use microphone") { model.startMic() }
                }
                if model.micDenied {
                    Text("Mic denied — sliders still work.")
                        .foregroundColor(.secondary)
                }
                Spacer()
                Toggle("Dark", isOn: $model.darkBackdrop)
            }

            HStack {
                Button("Present confirm island") {
                    model.host.present(content: PlaceholderConfirmCard())
                }
                Button("Present plan island") {
                    model.host.present(content: PlaceholderPlanSteps())
                }
                Button("Dismiss island") {
                    model.host.dismiss()
                }
            }

            HStack {
                Text("Presence:")
                ForEach(["turn.listening", "turn.thinking", "tool.started", "turn.needs_confirm", "turn.completed", "turn.failed"], id: \.self) { e in
                    Button(e.split(separator: ".").last.map(String.init) ?? e) {
                        model.host.handlePresenceEvent(named: e)
                        model.state = model.host.orbView.state
                    }
                }
            }
            .font(.caption)

            HStack {
                Text(String(format: "CPU %.2f%%", model.cpuPct)).monospacedDigit()
                Text(String(format: "%.0f fps", model.fps)).monospacedDigit()
                Text(model.gpuMs >= 0 ? String(format: "GPU %.3f ms", model.gpuMs) : "GPU n/a").monospacedDigit()
                Spacer()
                Text("level→\(model.state == .speaking ? "output" : "input")").foregroundColor(.secondary)
            }
            .font(.callout)
        }
        .padding()
        .onAppear {
            model.start()
            model.applyState()
        }
        .onDisappear { model.stop() }
    }
}
