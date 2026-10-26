import Carbon
import CoreGraphics
import Foundation

/// Listen-only Option-key tap: the single CGEventTap implementation shared
/// by the app (`OptionGestureTap`) and the owner calibration tool.
///
/// - `.listenOnly`: observes, never consumes — the callback always returns
///   the event unretained.
/// - Translates tap events into `GestureEngine` inputs with monotonic ms
///   stamps; the engine (not this source) decides what anything means.
/// - Re-enables automatically when macOS disables the tap and reports the
///   disable so the owner can count storms.
/// - Never created in checks (P3): pure engine tests inject synthetic
///   inputs instead.
public final class GestureTapSource {
    public enum State: String, Sendable {
        case stopped
        case running
        case noPermission
    }

    /// Every observed input, in tap order.
    public var onInput: ((PresenceGesture.Input) -> Void)?
    /// macOS disabled the tap (re-enabled automatically).
    public var onTapDisabled: (() -> Void)?

    public private(set) var state: State = .stopped

    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private let baseMs: Int = GestureTapSource.nowMs()

    // Keycodes (uniform across layouts for modifiers/esc):
    // 58 = left Option, 61 = right Option, 53 = Escape.
    public static let leftOptionKeyCode: CGKeyCode = 58
    public static let rightOptionKeyCode: CGKeyCode = 61
    public static let escapeKeyCode: CGKeyCode = 53

    public init() {}

    public static func inputMonitoringGranted() -> Bool {
        CGPreflightListenEventAccess()
    }

    public static func secureInputEnabled() -> Bool {
        IsSecureEventInputEnabled()
    }

    public func physicalOptionDown() -> Bool {
        CGEventSource.keyState(.combinedSessionState, key: Self.leftOptionKeyCode)
            || CGEventSource.keyState(.combinedSessionState, key: Self.rightOptionKeyCode)
    }

    /// Install the tap. Returns false (state `.noPermission`) when Input
    /// Monitoring is not granted — the caller falls back to the hotkey.
    @discardableResult
    public func start() -> Bool {
        guard Self.inputMonitoringGranted() else {
            state = .noPermission
            return false
        }
        teardown()
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
            return false
        }
        self.tap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        if let source = runLoopSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        }
        CGEvent.tapEnable(tap: tap, enable: true)
        state = .running
        return true
    }

    public func stop() {
        teardown()
        state = .stopped
    }

    private func teardown() {
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
        let me = Unmanaged<GestureTapSource>.fromOpaque(refcon).takeUnretainedValue()
        me.handleTapEvent(type: type, event: event)
        // Listen-only: the event continues to the system untouched.
        return Unmanaged.passUnretained(event)
    }

    private func handleTapEvent(type: CGEventType, event: CGEvent) {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let tap {
                CGEvent.tapEnable(tap: tap, enable: true)
            }
            onTapDisabled?()
            return
        default:
            break
        }
        guard state == .running else { return }
        let now = Self.stamp(since: baseMs)
        let flags = event.flags
        switch type {
        case .flagsChanged:
            let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
            if keyCode == Self.leftOptionKeyCode || keyCode == Self.rightOptionKeyCode {
                let side: PresenceGesture.OptionSide =
                    keyCode == Self.leftOptionKeyCode ? .left : .right
                if flags.contains(.maskAlternate) {
                    let exclusive = flags.intersection([.maskCommand, .maskControl, .maskShift]).isEmpty
                    onInput?(.optionDown(side: side, at: now, exclusive: exclusive))
                } else {
                    onInput?(.optionUp(side: side, at: now))
                    if flags.intersection([.maskAlternate, .maskCommand, .maskControl, .maskShift]).isEmpty {
                        onInput?(.modifiersReleased(at: now))
                    }
                }
            } else if flags.intersection([.maskAlternate, .maskCommand, .maskControl, .maskShift]).isEmpty {
                onInput?(.modifiersReleased(at: now))
            } else {
                onInput?(.extraModifier(at: now))
            }
        case .keyDown:
            let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
            if keyCode == Self.escapeKeyCode {
                onInput?(.escape(at: now))
            } else {
                onInput?(.otherKey(at: now))
            }
        case .keyUp:
            break // releases carry no gesture information (Option uses flagsChanged)
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            onInput?(.mouseDown(at: now))
        case .scrollWheel:
            onInput?(.scroll(at: now))
        default:
            break
        }
    }

    public var nowStamp: Int { Self.stamp(since: baseMs) }

    private static func nowMs() -> Int {
        Int(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 1_000_000)
    }

    private static func stamp(since base: Int) -> Int {
        max(0, nowMs() - base)
    }
}
