import AppKit
import Foundation

/// Content hosted inside the expanded island. Implemented by CLI 2's HUD
/// views (plan steps, captions, confirm card); placeholder implementations
/// for previews live in `PlaceholderIslandContent.swift`.
public protocol IslandContent: AnyObject {
    /// The content view, installed into the island's content slot.
    var view: NSView { get }
    /// Preferred content height in points (island clamps to 64...320 total).
    var contentHeight: CGFloat { get }
    /// Stable identifier for logging ("plan-steps", "caption", "confirm", ...).
    var contentID: String { get }
}

/// Notch-island panel host (R6): a non-activating `NSPanel` above the menu
/// bar that shows the compact orb and morphs into an island to host content.
///
/// - Never steals focus (`.nonactivatingPanel`), joins all Spaces, floats
///   above the menu bar (`.statusBar` level).
/// - Click-through while compact (`ignoresMouseEvents`); interactive only
///   when expanded with controls.
/// - Auto-hides over fullscreen apps unless the state is `.needsConfirm`.
/// - Follows the key window's screen; re-layouts on display changes.
public final class IslandHost: NSObject {
    /// Floating-orb placement on notch-less screens (owner setting).
    public var placement: IslandPlacement = .topCenter

    /// The orb view (full-bleed compact, badge expanded). Drive its `state`
    /// and `levelSource` directly, or feed presence events via `setState`.
    public let orbView = EvieOrbView()

    public private(set) var isExpanded = false
    public private(set) var currentContent: IslandContent?

    private let panel: NSPanel
    private let container = NSView()
    private let islandBackground: NSView
    private let contentSlot = NSView()
    private var observers: [NSObjectProtocol] = []

    public override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 40, height: 40),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        if FramePacer.reduceTransparency {
            let plain = NSView()
            plain.wantsLayer = true
            plain.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
            plain.layer?.cornerRadius = 20
            islandBackground = plain
        } else {
            let fx = NSVisualEffectView()
            fx.material = .hudWindow
            fx.state = .active
            fx.wantsLayer = true
            fx.layer?.cornerRadius = 20
            fx.layer?.masksToBounds = true
            islandBackground = fx
        }
        super.init()

        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.ignoresMouseEvents = true
        panel.contentView = container

        container.addSubview(islandBackground)
        container.addSubview(orbView)
        container.addSubview(contentSlot)
        islandBackground.isHidden = true
        contentSlot.isHidden = true

        let center = NotificationCenter.default
        let workspaceCenter = NSWorkspace.shared.notificationCenter
        observers = [
            center.addObserver(
                forName: NSApplication.didChangeScreenParametersNotification,
                object: nil, queue: .main
            ) { [weak self] _ in self?.updateLayout(animated: false) },
            workspaceCenter.addObserver(
                forName: NSWorkspace.didActivateApplicationNotification,
                object: nil, queue: .main
            ) { [weak self] _ in self?.updateLayout(animated: false) },
            workspaceCenter.addObserver(
                forName: NSWorkspace.screensDidSleepNotification,
                object: nil, queue: .main
            ) { [weak self] _ in self?.orbView.isVisible = false },
            workspaceCenter.addObserver(
                forName: NSWorkspace.screensDidWakeNotification,
                object: nil, queue: .main
            ) { [weak self] _ in self?.syncOcclusionVisibility() },
            center.addObserver(
                forName: NSWindow.didChangeOcclusionStateNotification,
                object: panel, queue: .main
            ) { [weak self] _ in self?.syncOcclusionVisibility() },
        ]
        updateLayout(animated: false)
    }

    deinit {
        for o in observers {
            NotificationCenter.default.removeObserver(o)
            NSWorkspace.shared.notificationCenter.removeObserver(o)
        }
    }

    /// Feed a presence event straight to the orb (mapping in one place).
    public func handlePresenceEvent(named eventName: String, panicked: Bool = false) {
        if let state = PresenceMapping.orbState(for: eventName, panicked: panicked) {
            orbView.state = state
        }
        updateLayout(animated: false)
    }

    /// Expand into an island hosting `content` (CLI 2's views).
    /// `showWindow: false` lays out without ordering front (headless checks).
    public func present(content: IslandContent, animated: Bool = true, showWindow: Bool = true) {
        isExpanded = true
        currentContent = content
        contentSlot.subviews.forEach { $0.removeFromSuperview() }
        contentSlot.addSubview(content.view)
        contentSlot.isHidden = false
        islandBackground.isHidden = false
        panel.ignoresMouseEvents = false
        updateLayout(
            animated: animated,
            mode: .expanded(contentHeight: content.contentHeight)
        )
        if showWindow {
            panel.orderFrontRegardless()
        }
    }

    /// Collapse back to the compact orb.
    public func dismiss(animated: Bool = true) {
        isExpanded = false
        currentContent = nil
        contentSlot.subviews.forEach { $0.removeFromSuperview() }
        contentSlot.isHidden = true
        islandBackground.isHidden = true
        panel.ignoresMouseEvents = true
        updateLayout(animated: animated, mode: .compact)
    }

    /// Re-resolve screen/fullscreen and re-apply the current mode's frame.
    public func updateLayout(animated: Bool) {
        let mode: IslandMode = isExpanded
            ? .expanded(contentHeight: currentContent?.contentHeight ?? 0)
            : .compact
        updateLayout(animated: animated, mode: mode)
    }

    /// The panel (read-only; for app integration, not for restyling).
    public var window: NSPanel { panel }

    /// Current screen geometry as resolved from live `NSScreen` values.
    public func currentScreenGeometry() -> ScreenGeometry? {
        guard let screen = targetScreen() else { return nil }
        let auxL: CGRect?
        let auxR: CGRect?
        if #available(macOS 12, *) {
            auxL = screen.auxiliaryTopLeftArea
            auxR = screen.auxiliaryTopRightArea
        } else {
            auxL = nil
            auxR = nil
        }
        return Self.geometry(
            frame: screen.frame,
            visibleFrame: screen.visibleFrame,
            safeAreaTop: screen.safeAreaInsets.top,
            auxLeft: auxL,
            auxRight: auxR
        )
    }

    /// Pure notch detection over raw screen values (unit-testable).
    ///
    /// Grounded on a 16" M2 (1710x1112 scaled): aux areas
    /// `(0,1074.5,751x37.5)` / `(959,1074.5,751x37.5)` leave a 208 pt gap,
    /// `safeAreaInsets.top` is 37.5. Rule: a center gap > 50 pt between the
    /// aux areas is a notch with half-width gap/2; else a positive top safe
    /// area means a notch of fallback half-width 104; else no notch.
    public static func geometry(
        frame: CGRect,
        visibleFrame: CGRect,
        safeAreaTop: CGFloat,
        auxLeft: CGRect?,
        auxRight: CGRect?
    ) -> ScreenGeometry {
        if let l = auxLeft, let r = auxRight {
            let gap = r.minX - l.maxX
            if gap > 50 {
                return ScreenGeometry(
                    frame: frame, visibleFrame: visibleFrame,
                    hasNotch: true, notchHalfWidth: gap / 2
                )
            }
        }
        if safeAreaTop > 0 {
            return ScreenGeometry(
                frame: frame, visibleFrame: visibleFrame,
                hasNotch: true, notchHalfWidth: 104
            )
        }
        return ScreenGeometry(
            frame: frame, visibleFrame: visibleFrame,
            hasNotch: false, notchHalfWidth: 0
        )
    }

    // MARK: - Private

    private func targetScreen() -> NSScreen? {
        NSApp?.keyWindow?.screen ?? NSScreen.main ?? NSScreen.screens.first
    }

    private func updateLayout(animated: Bool, mode: IslandMode) {
        guard let screen = targetScreen(),
              let geometry = currentScreenGeometry()
        else { return }
        let layout = IslandLayout.frame(
            screen: geometry,
            mode: mode,
            placement: placement,
            fullscreenApp: isFullscreenActive(on: screen),
            state: orbView.state
        )
        if layout.hidden {
            panel.orderOut(nil)
            orbView.isVisible = false
            return
        }
        orbView.isVisible = true
        syncOcclusionVisibility()
        let apply = {
            self.panel.setFrame(layout.rect, display: true)
            self.layoutContainer(mode: mode, panelSize: layout.rect.size)
        }
        if animated {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.32
                ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                self.panel.animator().setFrame(layout.rect, display: true)
            }
            layoutContainer(mode: mode, panelSize: layout.rect.size)
        } else {
            apply()
        }
    }

    private func layoutContainer(mode: IslandMode, panelSize: CGSize) {
        container.frame = NSRect(origin: .zero, size: panelSize)
        islandBackground.frame = container.bounds
        switch mode {
        case .compact:
            orbView.frame = container.bounds
        case .expanded:
            // Orb badge at leading, content fills the rest.
            let badge: CGFloat = 28
            orbView.frame = NSRect(x: 12, y: panelSize.height - 12 - badge, width: badge, height: badge)
            contentSlot.frame = NSRect(
                x: 12 + badge + 10, y: 10,
                width: panelSize.width - (12 + badge + 10) - 12,
                height: panelSize.height - 20
            )
            contentSlot.subviews.forEach { $0.frame = contentSlot.bounds }
        }
    }

    private func syncOcclusionVisibility() {
        orbView.isVisible = panel.occlusionState.contains(.visible)
    }

    /// True when the frontmost app covers the whole screen (fullscreen Space).
    /// Compares window SIZE only: CG window coordinates are top-left origin
    /// while AppKit is bottom-left, but sizes agree. Maximized-but-not-
    /// fullscreen windows match `visibleFrame`, not `frame`, which is what
    /// distinguishes them. Read-only window-list query; no new permission.
    /// Edge: two identical-size screens both hide when one is fullscreen.
    private func isFullscreenActive(on screen: NSScreen) -> Bool {
        guard let infos = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] else {
            return false
        }
        let ourPID = NSRunningApplication.current.processIdentifier
        let frontPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        for info in infos {
            guard let ownerPID = info[kCGWindowOwnerPID as String] as? pid_t,
                  ownerPID != ourPID,
                  ownerPID == frontPID,
                  let layer = info[kCGWindowLayer as String] as? Int, layer == 0,
                  let boundsDict = info[kCGWindowBounds as String] as? [String: Any],
                  let w = boundsDict["Width"] as? CGFloat,
                  let h = boundsDict["Height"] as? CGFloat
            else { continue }
            if abs(w - screen.frame.width) < 1, abs(h - screen.frame.height) < 1 {
                return true
            }
        }
        return false
    }
}
