import AppKit
import CoreVideo
import Foundation

/// Pure frame-pacing decisions (R4). The display link (`FramePacer`) delivers
/// ticks; the host (`EvieOrbView`) renders only when this policy says so, so
/// static states draw zero frames and invisible orbs cost nothing.
public enum FramePacingPolicy {
    /// Target render rate. Static states are 0 (cached frame). Low Power Mode
    /// caps animated states at 15 fps — a deliberate reading of "pause where
    /// applicable": freezing the listening indicator entirely would break
    /// voice-loop feedback, so the orb stays alive at quarter rate instead.
    public static func targetFPS(for state: OrbState, lowPowerMode: Bool) -> Int {
        if isStaticState(state) { return 0 }
        if lowPowerMode { return 15 }
        switch state {
        case .listening, .speaking:
            return 60
        case .thinking, .acting, .needsConfirm, .success, .error:
            return 30
        case .idle, .offline:
            return 0
        }
    }

    public static func isStaticState(_ state: OrbState) -> Bool {
        state == .idle || state == .offline
    }

    /// Whether the display link should run at all. A settled static orb stops
    /// the link: zero frames, ~zero CPU. Any state/level/visibility change
    /// restarts it.
    public static func linkShouldRun(
        state: OrbState, settled: Bool, levelActive: Bool, visible: Bool
    ) -> Bool {
        guard visible else { return false }
        if !isStaticState(state) { return true }
        if !settled { return true }
        return levelActive
    }

    /// Per-tick render decision. `visible` is false when hidden, occluded
    /// (`NSWindow.occlusionState`), or the screen is asleep.
    public static func shouldRender(
        state: OrbState, visible: Bool, paramsChanged: Bool, levelActive: Bool
    ) -> Bool {
        guard visible else { return false }
        if !isStaticState(state) { return true }
        return paramsChanged || levelActive
    }
}

/// Display-link tick source. CVDisplayLink is the pacing API for the macOS 14
/// deployment target (no newer AppKit replacement exists). Delivers thinned
/// ticks on a private high-priority thread; opens no windows.
public final class FramePacer: @unchecked Sendable {
    /// Desired ticks per second; 0 selects a 4 Hz watchdog for static states
    /// (the host's policy still skips rendering when settled).
    public var targetFPS: Int = 60
    /// Set false when hidden, occluded, or the screen sleeps: no ticks at all.
    public var isVisible: Bool = true
    /// Called on each delivered tick (link thread, not main).
    public var onTick: ((CVTimeStamp) -> Void)?

    private var link: CVDisplayLink?
    private let lock = NSLock()
    private var tickCount: UInt64 = 0
    private var running = false

    public init() {}

    /// Display refresh rate in Hz (60, 120 ProMotion, ...), 60 if unknown.
    public var displayHz: Double {
        guard let link else { return 60 }
        let period = CVDisplayLinkGetActualOutputVideoRefreshPeriod(link)
        guard period > 0 else { return 60 }
        let hz = 1.0 / period
        guard hz > 10, hz < 500 else { return 60 }
        return hz
    }

    public var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return running
    }

    public func start() {
        lock.lock()
        if running {
            lock.unlock()
            return
        }
        if link == nil {
            var created: CVDisplayLink?
            CVDisplayLinkCreateWithActiveCGDisplays(&created)
            link = created
            if let link {
                let ctx = Unmanaged.passUnretained(self).toOpaque()
                CVDisplayLinkSetOutputCallback(link, { _, _, _, _, _, ctx in
                    guard let ctx else { return kCVReturnSuccess }
                    Unmanaged<FramePacer>.fromOpaque(ctx).takeUnretainedValue().tick()
                    return kCVReturnSuccess
                }, ctx)
            }
        }
        guard let link else {
            lock.unlock()
            return
        }
        running = (CVDisplayLinkStart(link) == kCVReturnSuccess)
        lock.unlock()
    }

    public func stop() {
        lock.lock()
        if let link, running {
            CVDisplayLinkStop(link)
        }
        running = false
        lock.unlock()
    }

    private func tick() {
        let fps = max(targetFPS, 4)
        lock.lock()
        tickCount += 1
        let n = tickCount
        let visible = isVisible
        let handler = onTick
        lock.unlock()
        guard visible, let handler else { return }
        // Thin display-rate ticks down to the target rate.
        let skip = max(1, Int((displayHz / Double(fps)).rounded()))
        guard n % UInt64(skip) == 0 else { return }
        handler(CVTimeStamp())
    }

    // MARK: - System state readers for the host

    public static var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    public static var reduceTransparency: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency
    }

    public static var lowPowerMode: Bool {
        ProcessInfo.processInfo.isLowPowerModeEnabled
    }
}
