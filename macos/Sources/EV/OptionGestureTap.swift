import AppKit
import CoreGraphics
import Foundation
import PresenceGesture

/// Option-key gesture source for the EV app, over the shared listen-only
/// tap (`GestureTapSource`, also used by the calibration tool).
///
/// Behavior contract:
/// - Installed only when the owner enables `EV_GESTURE_ENABLED` (default
///   OFF). Until the guided check (D7) runs, no tap is ever created.
/// - Requires Input Monitoring. Without it the tap is not created and
///   `state` reports `.noPermission` — the app keeps using the existing
///   `GlobalHotkey` + menu.
/// - When macOS disables the tap it is re-enabled by the source; rapid
///   disable storms suspend the engine instead of spinning.
/// - Secure event input (password fields) suspends the engine with an
///   honest state the HUD can show; listening resumes when it clears.
/// - A watchdog timer feeds `tick` (hold/latch timing) and `resync` with
///   the physical Option state (lost key-up repair).
///
/// Coexists with `GlobalHotkey`: neither consumes events, so both can run.
final class OptionGestureTap {
    enum State: String {
        case off // setting disabled (default)
        case noPermission // Input Monitoring not granted
        case running
        case suspendedSecureInput // password field focused somewhere
        case tapDisabled // storm guard tripped, retrying
    }

    /// Emitted for every batch of engine outputs. Host drives turns from
    /// `.startListening` / `.stopListening` / `.cancelTurn`.
    var onOutputs: (([PresenceGesture.Output]) -> Void)?
    /// Emitted on state transitions so the HUD can note fallback honestly.
    var onStateChange: ((State) -> Void)?

    private(set) var state: State = .off {
        didSet { if oldValue != state { onStateChange?(state) } }
    }

    /// Engine stats for calibration logs and diagnostics. Never user-facing.
    var engineStats: PresenceGesture.Stats { engine.stats }
    var engineStateDescription: String { engine.stateDescription }
    var isListening: Bool { engine.isListening }

    private var engine: GestureEngine
    private let source = GestureTapSource()
    private var watchdog: Timer?
    private var retryWork: DispatchWorkItem?
    private var disableStamps: [TimeInterval] = []
    private var secureSuspended = false

    init() {
        var settings = PresenceGesture.Settings()
        settings.enabled = true // engine runs; the TAP is gated by the setting below
        let defaults = UserDefaults.standard
        switch (defaults.string(forKey: "EV_GESTURE_OPTION_SIDE") ?? "any").lowercased() {
        case "left": settings.optionScope = .left
        case "right": settings.optionScope = .right
        default: settings.optionScope = .any
        }
        if defaults.object(forKey: "EV_GESTURE_TAP_LATCH") != nil {
            settings.tapLatchEnabled = defaults.bool(forKey: "EV_GESTURE_TAP_LATCH")
        }
        engine = GestureEngine(settings: settings)
        source.onInput = { [weak self] input in
            guard let self, self.state == .running else { return }
            self.feed(input)
        }
        source.onTapDisabled = { [weak self] in self?.noteTapDisabled() }
    }

    static var settingEnabled: Bool {
        UserDefaults.standard.bool(forKey: "EV_GESTURE_ENABLED")
    }

    // MARK: - Lifecycle

    /// No-op unless `EV_GESTURE_ENABLED` is set. Safe to call at app start.
    func start() {
        guard Self.settingEnabled else {
            state = .off
            return
        }
        guard source.start() else {
            state = .noPermission
            NSLog("[GESTURE] Input Monitoring not granted; hotkey remains the front door")
            return
        }
        state = .running
        startWatchdog()
        NSLog("[GESTURE] listen-only tap installed")
    }

    func stop() {
        source.stop()
        retryWork?.cancel()
        retryWork = nil
        watchdog?.invalidate()
        watchdog = nil
        secureSuspended = false
        state = .off // stopped means off until start()
    }

    private func noteTapDisabled() {
        let now = ProcessInfo.processInfo.systemUptime
        disableStamps = disableStamps.filter { now - $0 < 10 } + [now]
        guard disableStamps.count > 5 else { return }
        // Storm: stop spinning on a dying tap; suspend and retry later.
        disableStamps.removeAll()
        source.stop()
        feed(.suspend(at: source.nowStamp))
        state = .tapDisabled
        NSLog("[GESTURE] tap disable storm; suspended with hotkey fallback, retry in 5s")
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.feed(.resume(at: self.source.nowStamp))
            if self.source.start() {
                self.state = .running
            } else {
                self.state = .noPermission
            }
        }
        retryWork?.cancel()
        retryWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }

    // MARK: - Watchdog (tick + resync + secure-input)

    private func startWatchdog() {
        watchdog?.invalidate()
        let timer = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
            self?.watchdogFired()
        }
        watchdog = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func watchdogFired() {
        guard state == .running || state == .suspendedSecureInput else { return }
        let now = source.nowStamp
        // Secure event input (password fields): suspend with an honest state.
        let secured = GestureTapSource.secureInputEnabled()
        if secured, !secureSuspended {
            secureSuspended = true
            feed(.suspend(at: now))
            state = .suspendedSecureInput
            NSLog("[GESTURE] secure event input; suspended with hotkey fallback")
            return
        }
        if !secured, secureSuspended {
            secureSuspended = false
            feed(.resume(at: now))
            state = .running
            NSLog("[GESTURE] secure input cleared; resumed")
            return
        }
        guard !secureSuspended else { return }
        feed(.tick(at: now))
        // Lost key-up repair: adopt the physical truth, never auto-activate
        // (the engine blocks on uncertain state by construction).
        feed(.resync(optionPhysicalDown: source.physicalOptionDown(), at: now))
    }

    // MARK: - Helpers

    private func feed(_ input: PresenceGesture.Input) {
        let outputs = engine.handle(input)
        if !outputs.isEmpty {
            onOutputs?(outputs)
        }
    }
}
