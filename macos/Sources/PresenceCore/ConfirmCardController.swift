import AppKit
import SwiftUI

/// Hosts the confirm card in a key-capable panel.
///
/// Unlike the pill (glanceable, never key), the card takes keyboard input:
/// Return approves, Esc denies. macOS delivers key events only to an active
/// app, so showing the card promotes the app via `onBeforeShow` and hiding
/// restores it via `onAfterHide` (the host plugs the same
/// regular/accessory dance the menu panel uses). Clicks work regardless.
/// The card never activates anything by itself and never steals focus on a
/// path the host did not bless: both hooks are required to show.
@MainActor
public final class ConfirmCardController {
    /// Required: promote the app so Return/Esc reach the card.
    public var onBeforeShow: (() -> Void)?
    /// Required: restore the app's background state after hide.
    public var onAfterHide: (() -> Void)?
    public var onDecision: ((ConfirmCardModel.Phase) -> Void)?

    private var panel: NSPanel?
    private var model: ConfirmCardModel?
    private var keyMonitor: Any?
    private var cancellable: AnyObject?

    public init() {}

    public var isShown: Bool { panel?.isVisible == true }
    public var shownTicketId: String? { model?.ticket.ticketId }

    /// Show the card for a ticket. Returns false (and shows nothing) when
    /// the activation hooks are missing — a card that cannot take keys is
    /// worse than no card, because Return would fall through to the
    /// frontmost app.
    @discardableResult
    public func show(ticket: NeedsConfirm, client: ConfirmClient) -> Bool {
        guard onBeforeShow != nil, onAfterHide != nil else { return false }
        hide()
        let model = ConfirmCardModel(ticket: ticket, client: client)
        self.model = model
        onBeforeShow?()
        let view = ConfirmCardView(model: model)
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: ConfirmCardMetrics.width, height: ConfirmCardMetrics.height),
            styleMask: [.titled, .closable, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.title = "Confirm"
        panel.contentView = NSHostingView(rootView: view)
        position(panel)
        self.panel = panel
        // Dismiss on decision: the stream's terminal events update the pill;
        // the card closes once its own decision lands (or fails honestly).
        let token = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.checkDecision() }
        }
        cancellable = token
        model.startClock()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.isShown else { return event }
            if event.keyCode == 36 { // Return
                Task { @MainActor in await self.model?.approve() }
                return nil
            }
            if event.keyCode == 53 { // Esc
                Task { @MainActor in await self.model?.deny() }
                return nil
            }
            return event
        }
        panel.orderFrontRegardless()
        panel.makeKey()
        return true
    }

    public func hide() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        (cancellable as? Timer)?.invalidate()
        cancellable = nil
        model?.stopClock()
        let wasShown = isShown
        panel?.orderOut(nil)
        panel = nil
        model = nil
        if wasShown {
            onAfterHide?()
        }
    }

    private func checkDecision() {
        guard let model else { return }
        switch model.phase {
        case .approved, .denied:
            onDecision?(model.phase)
            // Brief beat so the result line is visible, then close.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                Task { @MainActor in self?.hide() }
            }
            (cancellable as? Timer)?.invalidate()
            cancellable = nil
        case .failed:
            onDecision?(model.phase)
            // Failures stay open: the owner reads the honest reason and
            // dismisses (or retries deny) deliberately.
            (cancellable as? Timer)?.invalidate()
            cancellable = nil
        case .pending, .working:
            break
        }
    }

    private func position(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(
            x: visible.midX - ConfirmCardMetrics.width / 2,
            y: visible.midY - ConfirmCardMetrics.height / 2 + visible.height * 0.08
        ))
    }
}
