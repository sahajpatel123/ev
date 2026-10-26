import AppKit
import Carbon
import CoreGraphics
import Foundation
import PresenceGesture

/// Listen-only Option-key gesture source for the EV app.
///
/// Owns a `CGEventTap` in `.listenOnly` mode: it observes key/flag/mouse
/// events and NEVER consumes or modifies them (the callback always returns
/// the event unretained). Each observation becomes a `GestureEngine` input
/// with a monotonic millisecond stamp; engine outputs flow to `onOutputs`.
///
/// Behavior contract:
/// - Installed only when the owner enables `EV_GESTURE_ENABLED` (default
///   OFF). Until the guided check (D7) runs, this class never creates a tap.
/// - Requires Input Monitoring (`CGPreflightListenEventAccess`). Without it
///   the tap is not created and `state` reports `.noPermission` — the app
///   keeps using the existing `GlobalHotkey` + menu.
/// - When macOS disables the tap (timeout/user input) it is re-enabled;
///   rapid disable storms suspend the engine instead of spinning.
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
    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var watchdog: Timer?
    private var retryWork: DispatchWorkItem?
    private var disableStamps: [TimeInterval] = []
    private var secureSuspended = false
    private let startUptimeMs: Int = OptionGestureTap.nowMs()

    // Keycodes (HID usage page 7, uniform across layouts for modifiers):
    // 58 = left Option, 61 = right Option, 53 = Escape.
    private static let leftOptionKeyCode: CGKeyCode = 58
    private static let rightOptionKeyCode: CGKeyCode = 61
    private static let escapeKeyCode: CGKeyCode = 53

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
        guard CGPreflightListenEventAccess() else {
            state = .noPermission
            NSLog("[GESTURE] Input Monitoring not granted; hotkey remains the front door")
            return
        }
        installTap()
    }

    func stop() {
        teardownTap()
        retryWork?.cancel()
        retryWork = nil
        watchdog?.invalidate()
        watchdog = nil
        secureSuspended = false
        state = .off // stopped means off until start()
    }

    // MARK: - Tap

    private func installTap() {
        teardownTap()
        let mask: CGEventMask =
            (1 << CGEventType.keyDown.rawValue)
            | (1 << CGEventType.keyUp.rawValue)
            | (1 << CGEventType.flagsChanged.rawValue)
            | (1 << CGEventType.leftMouseDown.rawValue)
            | (1 << CGEventType.rightMouseDown.rawValue)
            | (1 << CGEventType.otherMouseDown.rawValue)
            | (1 << CGEventType.scrollWheel.rawValue)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: CGEventMask(mask),
            callback: Self.tapCallback,
            userInfo: refcon
        ) else {
            state = .noPermission
            NSLog("[GESTURE] tapCreate failed; hotkey remains the front door")
            return
        }
        self.tap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        if let source = runLoopSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        }
        CGEvent.tapEnable(tap: tap, enable: true)
        state = .running
        startWatchdog()
        NSLog("[GESTURE] listen-only tap installed")
    }

    private func teardownTap() {
        if let tap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        if let tap {
            CFMachPortInvalidate(tap)
        }
        tap = nil
        runLoopSource = nil
    }

    private static let tapCallback: CGEventTapCallBack = { _, type, event, refcon in
        guard let refcon else { return Unmanaged.passUnretained(event) }
        let me = Unmanaged<OptionGestureTap>.fromOpaque(refcon).takeUnretainedValue()
        me.handleTapEvent(type: type, event: event)
        // Listen-only: the event continues to the system untouched.
        return Unmanaged.passUnretained(event)
    }

    private func handleTapEvent(type: CGEventType, event: CGEvent) {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            noteTapDisabled()
            return
        default:
            break
        }
        guard state == .running else { return }
        let now = Self.stamp(since: startUptimeMs)
        let flags = event.flags
        switch type {
        case .flagsChanged:
            let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
            if keyCode == Self.leftOptionKeyCode || keyCode == Self.rightOptionKeyCode {
                let side: PresenceGesture.OptionSide =
                    keyCode == Self.leftOptionKeyCode ? .left : .right
                if flags.contains(.maskAlternate) {
                    let exclusive = flags.intersection([.maskCommand, .maskControl, .maskShift]).isEmpty
                    feed(.optionDown(side: side, at: now, exclusive: exclusive))
                } else {
                    feed(.optionUp(side: side, at: now))
                    if flags.intersection([.maskAlternate, .maskCommand, .maskControl, .maskShift]).isEmpty {
                        feed(.modifiersReleased(at: now))
                    }
                }
            } else {
                // Some other modifier changed: a chord while Option is held,
                // typing noise otherwise (the engine decides by state).
                if flags.intersection([.maskAlternate, .maskCommand, .maskControl, .maskShift]).isEmpty {
                    feed(.modifiersReleased(at: now))
                } else {
                    feed(.extraModifier(at: now))
                }
            }
        case .keyDown:
            let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
            if keyCode == Self.escapeKeyCode {
                feed(.escape(at: now))
            } else {
                feed(.otherKey(at: now))
            }
        case .keyUp:
            break // releases carry no gesture information (Option uses flagsChanged)
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            feed(.mouseDown(at: now))
        case .scrollWheel:
            feed(.scroll(at: now))
        default:
            break
        }
    }

    private func noteTapDisabled() {
        guard let tap else { return }
        CGEvent.tapEnable(tap: tap, enable: true)
        let now = ProcessInfo.processInfo.systemUptime
        disableStamps = disableStamps.filter { now - $0 < 10 } + [now]
        if disableStamps.count > 5 {
            // Storm: stop spinning on a dying tap; suspend and retry later.
            disableStamps.removeAll()
            teardownTap()
            feed(.suspend(at: Self.stamp(since: startUptimeMs)))
            state = .tapDisabled
            NSLog("[GESTURE] tap disable storm; suspended with hotkey fallback, retry in 5s")
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.feed(.resume(at: Self.stamp(since: self.startUptimeMs)))
                self.installTap()
            }
            retryWork?.cancel()
            retryWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
        }
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
        let now = Self.stamp(since: startUptimeMs)
        // Secure event input (password fields): suspend with an honest state.
        let secured = IsSecureEventInputEnabled()
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
        let left = CGEventSource.keyState(.combinedSessionState, key: Self.leftOptionKeyCode)
        let right = CGEventSource.keyState(.combinedSessionState, key: Self.rightOptionKeyCode)
        feed(.resync(optionPhysicalDown: left || right, at: now))
    }

    // MARK: - Helpers

    private func feed(_ input: PresenceGesture.Input) {
        let outputs = engine.handle(input)
        if !outputs.isEmpty {
            onOutputs?(outputs)
        }
    }

    private static func nowMs() -> Int {
        Int(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 1_000_000)
    }

    private static func stamp(since base: Int) -> Int {
        max(0, nowMs() - base)
    }
}
