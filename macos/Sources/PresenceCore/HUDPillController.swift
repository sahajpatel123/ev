import AppKit
import Combine
import SwiftUI

/// Hosts the HUD pill in a small non-activating panel.
///
/// Panel contract (from `PresenceController`'s proven recipe):
/// - never takes focus (`.nonactivatingPanel`, shown with
///   `orderFrontRegardless`, never made key);
/// - click-through except its controls (the hosting view's `hitTest`
///   passes everything but buttons to the app below);
/// - visible on all Spaces, floats above normal windows;
/// - hidden while idle (no turn, no panic); shown otherwise.
/// Light/dark follows the system; Reduce Motion and Increase Contrast are
/// honored inside `HUDPillView`.
@MainActor
public final class HUDPillController {
    public var onReviewConfirm: () -> Void = {}
    public var onOpenPanel: () -> Void = {}

    private var panel: NSPanel?
    private var store: PresenceStore?
    private var cancellables = Set<AnyCancellable>()
    private var expanded = false
    private var lastTurnId: String?

    public init() {}

    public func attach(_ store: PresenceStore) {
        detach()
        self.store = store
        store.$snapshot
            .receive(on: DispatchQueue.main)
            .sink { [weak self] snapshot in self?.render(snapshot) }
            .store(in: &cancellables)
        render(store.snapshot)
    }

    public func detach() {
        cancellables.removeAll()
        store = nil
        panel?.orderOut(nil)
        panel = nil
    }

    /// For snapshot tests and previews: the exact root view the panel shows.
    public func rootView(for snapshot: PresenceSnapshot) -> some View {
        HUDPillRoot(
            snapshot: snapshot,
            expanded: expanded,
            onToggleExpanded: { [weak self] in self?.toggleExpanded() },
            onReviewConfirm: { [weak self] in self?.onReviewConfirm() },
            onDismiss: { [weak self] in self?.dismissTurn() }
        )
    }

    private func toggleExpanded() {
        expanded.toggle()
        if let snapshot = store?.snapshot { render(snapshot) }
    }

    private func dismissTurn() {
        expanded = false
        store?.dismissTurn()
    }

    private func render(_ snapshot: PresenceSnapshot) {
        if snapshot.turn?.id != lastTurnId {
            lastTurnId = snapshot.turn?.id
            expanded = false
        }
        guard snapshot.turn != nil || snapshot.panic.panicked else {
            panel?.orderOut(nil)
            return
        }
        let panel = ensurePanel()
        panel.contentView = HitTestHostingView(rootView: rootView(for: snapshot))
        let size = NSSize(
            width: HUDPillMetrics.width,
            height: expanded ? HUDPillMetrics.expandedHeight : HUDPillMetrics.compactHeight
        )
        panel.setContentSize(size)
        position(panel, size: size)
        panel.orderFrontRegardless()
    }

    private func ensurePanel() -> NSPanel {
        if let panel { return panel }
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: HUDPillMetrics.width, height: HUDPillMetrics.compactHeight),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        self.panel = panel
        return panel
    }

    private func position(_ panel: NSPanel, size: NSSize) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(
            x: visible.midX - size.width / 2,
            y: visible.maxY - size.height - 16
        ))
    }
}

/// Root view with environment defaults for captions. Separated so snapshot
/// tests render the identical tree the panel shows.
public struct HUDPillRoot: View {
    public var snapshot: PresenceSnapshot
    public var expanded: Bool
    public var onToggleExpanded: () -> Void
    public var onReviewConfirm: () -> Void
    public var onDismiss: () -> Void

    @AppStorage("EV_PRESENCE_CAPTIONS") private var captionsEnabled = true

    public init(
        snapshot: PresenceSnapshot,
        expanded: Bool,
        onToggleExpanded: @escaping () -> Void,
        onReviewConfirm: @escaping () -> Void,
        onDismiss: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.expanded = expanded
        self.onToggleExpanded = onToggleExpanded
        self.onReviewConfirm = onReviewConfirm
        self.onDismiss = onDismiss
    }

    public var body: some View {
        HUDPillView(
            snapshot: snapshot,
            captionsEnabled: captionsEnabled,
            expanded: expanded,
            onToggleExpanded: onToggleExpanded,
            onReviewConfirm: onReviewConfirm,
            onDismiss: onDismiss
        )
    }
}

/// Hosting view that is click-through except for real controls: only hits
/// that land on (or inside) an `NSButton` are kept; everything else passes
/// to the app below, so the pill never swallows clicks meant for work.
public final class HitTestHostingView<Content: View>: NSHostingView<Content> {
    override public func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        var cursor: NSView? = hit
        while let current = cursor, current != self {
            if current is NSButton { return hit }
            cursor = current.superview
        }
        return nil
    }
}
