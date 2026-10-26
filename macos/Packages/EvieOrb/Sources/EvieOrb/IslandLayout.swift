import CoreGraphics
import Foundation

/// A display's geometry in AppKit screen coordinates (origin bottom-left).
/// Mirrors the `NSScreen` values `IslandHost` reads; constructible by hand
/// so layout is unit-testable with simulated screens.
public struct ScreenGeometry: Sendable, Equatable {
    public var frame: CGRect
    public var visibleFrame: CGRect
    public var hasNotch: Bool
    /// Half the notch width in points (0 when `hasNotch` is false).
    public var notchHalfWidth: CGFloat

    public init(frame: CGRect, visibleFrame: CGRect, hasNotch: Bool, notchHalfWidth: CGFloat) {
        self.frame = frame
        self.visibleFrame = visibleFrame
        self.hasNotch = hasNotch
        self.notchHalfWidth = hasNotch ? notchHalfWidth : 0
    }
}

/// Floating-orb placement on notch-less screens (owner setting).
public enum IslandPlacement: String, Sendable, CaseIterable {
    case topCenter
    case nearMenuBar
    case bottomRight
}

public enum IslandMode: Sendable, Equatable {
    case compact
    case expanded(contentHeight: CGFloat)
}

public struct IslandFrame: Sendable, Equatable {
    /// Panel frame in screen coordinates (`.zero` when hidden).
    public var rect: CGRect
    /// Orb diameter to render (full size compact, badge size expanded).
    public var orbDiameter: CGFloat
    public var hidden: Bool
}

/// Pure notch-island layout (R6). No AppKit here — `IslandHost` feeds live
/// `NSScreen` geometries through this same function.
public enum IslandLayout {
    public static let compactDiameter: CGFloat = 40
    public static let expandedOrbDiameter: CGFloat = 28
    public static let islandWidth: CGFloat = 360
    public static let islandMinHeight: CGFloat = 64
    public static let islandMaxHeight: CGFloat = 320
    public static let islandPadding: CGFloat = 32
    public static let islandSideMargin: CGFloat = 16
    public static let notchGap: CGFloat = 10
    public static let topGap: CGFloat = 6
    public static let trailingInset: CGFloat = 8
    public static let bottomGap: CGFloat = 12
    public static let screenTopGap: CGFloat = 2

    public static func frame(
        screen: ScreenGeometry,
        mode: IslandMode,
        placement: IslandPlacement,
        fullscreenApp: Bool,
        state: OrbState
    ) -> IslandFrame {
        // Fullscreen apps: auto-hide unless confirmation is needed.
        if fullscreenApp, state != .needsConfirm {
            return IslandFrame(rect: .zero, orbDiameter: compactDiameter, hidden: true)
        }
        switch mode {
        case .compact:
            return IslandFrame(
                rect: compactRect(screen: screen, placement: placement),
                orbDiameter: compactDiameter,
                hidden: false
            )
        case .expanded(let contentHeight):
            let h = min(islandMaxHeight, max(islandMinHeight, contentHeight + islandPadding))
            return IslandFrame(
                rect: expandedRect(screen: screen, placement: placement, height: h),
                orbDiameter: expandedOrbDiameter,
                hidden: false
            )
        }
    }

    private static func compactRect(screen: ScreenGeometry, placement: IslandPlacement) -> CGRect {
        let d = compactDiameter
        if screen.hasNotch {
            // Beside the notch (right side), tucked under the screen top edge,
            // overlapping the menu-bar band like a Dynamic Island companion.
            let x = screen.frame.midX + screen.notchHalfWidth + notchGap
            let y = screen.frame.maxY - screenTopGap - d
            return CGRect(x: x, y: y, width: d, height: d)
        }
        switch placement {
        case .topCenter:
            return CGRect(
                x: screen.visibleFrame.midX - d / 2,
                y: screen.visibleFrame.maxY - topGap - d,
                width: d, height: d
            )
        case .nearMenuBar:
            return CGRect(
                x: screen.visibleFrame.maxX - trailingInset - d,
                y: screen.visibleFrame.maxY - topGap - d,
                width: d, height: d
            )
        case .bottomRight:
            return CGRect(
                x: screen.visibleFrame.maxX - trailingInset - d,
                y: screen.visibleFrame.minY + bottomGap,
                width: d, height: d
            )
        }
    }

    private static func expandedRect(
        screen: ScreenGeometry, placement: IslandPlacement, height: CGFloat
    ) -> CGRect {
        let w = min(islandWidth, screen.frame.width - islandSideMargin * 2)
        if screen.hasNotch {
            // Island drops from the notch: centered, top at the visible top.
            let x = screen.frame.midX - w / 2
            let y = screen.visibleFrame.maxY - height
            return CGRect(x: x, y: y, width: w, height: height)
        }
        switch placement {
        case .topCenter:
            return CGRect(
                x: screen.visibleFrame.midX - w / 2,
                y: screen.visibleFrame.maxY - height,
                width: w, height: height
            )
        case .nearMenuBar:
            return CGRect(
                x: screen.visibleFrame.maxX - w,
                y: screen.visibleFrame.maxY - height,
                width: w, height: height
            )
        case .bottomRight:
            // Bottom-anchored: grows upward.
            return CGRect(
                x: screen.visibleFrame.maxX - w,
                y: screen.visibleFrame.minY + bottomGap,
                width: w, height: height
            )
        }
    }
}
