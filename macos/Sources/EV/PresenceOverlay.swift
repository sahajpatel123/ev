import AppKit
import MapKit
import SwiftUI

// MARK: - Visual language

enum EVPalette {
    /// Whole window sits near-transparent; the card is glass, not a slab.
    static let windowOpacity: CGFloat = 0.92
    /// Barely-there pane tint so the desktop shows through the glass.
    static let paneFill = Color.white.opacity(0.08)
    static let ink = Color(red: 0.957, green: 0.937, blue: 0.902)
    static let muted = Color.white.opacity(0.46)
}


// MARK: - Catalogs (sizes / time-types / lookouts)

enum PresenceKind: String, CaseIterable {
    case card, briefing, list, conversation, map
    case chip, radar, vitals, horizon, scope, bench, trace, pulse, ticker, wire

    var label: String { rawValue }

    var defaultSize: PresenceSize {
        switch self {
        case .chip, .pulse: return .chip
        case .ticker: return .ticker
        case .briefing: return .brief
        case .conversation: return .ticker
        case .list, .trace: return .slate
        case .map: return .canvas
        case .radar, .vitals, .horizon, .scope, .bench, .wire: return .lookout
        case .card: return .card
        }
    }

    var defaultTime: PresenceTimeType {
        switch self {
        case .chip: return .flash
        case .ticker: return .glance
        case .card, .briefing: return .linger
        case .list, .map, .trace: return .hold
        case .conversation: return .glance
        case .pulse: return .pulse
        case .wire: return .session
        case .radar, .vitals, .horizon, .scope, .bench: return .lookout
        }
    }

    var defaultPlacement: PresencePlacement {
        switch self {
        case .radar, .chip: return .upperRight
        case .vitals: return .upperLeft
        case .horizon: return .lowerRight
        case .wire: return .lowerLeft
        case .scope: return .right
        case .bench: return .left
        case .pulse, .ticker, .conversation: return .top
        default: return .center
        }
    }

    var isLookoutDefault: Bool {
        switch self {
        case .radar, .vitals, .horizon, .scope, .bench, .wire: return true
        default: return false
        }
    }
}

enum PresenceSize: String {
    case pip, chip, card, brief, slate, canvas, lookout, ticker

    var panelSize: NSSize {
        switch self {
        case .pip: return NSSize(width: 180, height: 72)
        case .chip: return NSSize(width: 280, height: 148)
        case .card: return NSSize(width: 480, height: 300)
        case .brief: return NSSize(width: 560, height: 420)
        case .slate: return NSSize(width: 720, height: 520)
        case .canvas: return NSSize(width: 960, height: 680)
        case .lookout: return NSSize(width: 340, height: 460)
        case .ticker: return NSSize(width: 920, height: 88)
        }
    }
}

enum PresenceTimeType: String {
    case flash, glance, linger, hold, lookout, pulse, session

    var defaultTTL: TimeInterval? {
        switch self {
        case .flash: return 1.6
        case .glance: return 5
        case .linger: return 30
        case .pulse: return 12
        case .hold, .lookout, .session: return nil
        }
    }
}

enum PresencePlacement: String {
    case center
    case upperRight = "upper_right"
    case upperLeft = "upper_left"
    case lowerRight = "lower_right"
    case lowerLeft = "lower_left"
    case right, left, top, stack
}

struct PresenceContent {
    var id: String
    var title: String
    var message: String
    var kind: PresenceKind
    var size: PresenceSize
    var timeType: PresenceTimeType
    var placement: PresencePlacement
    var items: [String] = []
    var questions: [String] = []
    var response: String? = nil
    var recommendation: String? = nil
    var source: String? = nil
    var lookout: Bool = false
    var ttl: TimeInterval? = nil
    var layout: String? = nil
    var driftX: CGFloat? = nil
    var driftY: CGFloat? = nil
    var tilt: Double? = nil
    var origin: CLLocationCoordinate2D? = nil
    var destination: CLLocationCoordinate2D? = nil
    var image: NSImage? = nil

    var resolvedTTL: TimeInterval? { ttl ?? timeType.defaultTTL }
    var resolvedReply: String {
        let reply = (response ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return reply.isEmpty ? message : reply
    }
}

// MARK: - Controller

/// EVIE's native HUD — a transparent single-paragraph glass card.
/// No chrome or badges; TTL auto-dismisses, tap dismisses early.
/// `ev://dismiss?id=`, or `ev://dismiss-all`.
@MainActor
final class PresenceController {
    static let shared = PresenceController()

    private struct Slot {
        var content: PresenceContent
        var panel: NSPanel
        var hosting: NSHostingView<PresenceOverlayView>
        var dismissWork: DispatchWorkItem?
    }

    private var slots: [String: Slot] = [:]
    private(set) var lastContent: PresenceContent?
    private var stackIndex = 0

    /// Hosting view that never paints an opaque slab behind the glass.
    private final class ClearHostingView<Content: View>: NSHostingView<Content> {
        override var isOpaque: Bool { false }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            clearChrome()
        }

        override func layout() {
            super.layout()
            clearChrome()
        }

        private func clearChrome() {
            wantsLayer = true
            layer?.backgroundColor = NSColor.clear.cgColor
            layer?.isOpaque = false
            layer?.cornerRadius = 0
            layer?.masksToBounds = false
            window?.isOpaque = false
            window?.backgroundColor = .clear
            window?.alphaValue = EVPalette.windowOpacity
            superview?.wantsLayer = true
            superview?.layer?.backgroundColor = NSColor.clear.cgColor
        }
    }

    var lastContents: [PresenceContent] { slots.values.map(\.content) }

    /// Never steals focus: the card is glanceable glass with TTL auto-dismiss.
    func show(_ content: PresenceContent) {
        present(content, stealFocus: false)
    }

    func showVisor(jpeg: Data, title: String = "Look", message: String = "") {
        guard let image = NSImage(data: jpeg), image.size.width > 1 else { return }
        present(
            PresenceContent(
                id: "visor.camera",
                title: title,
                message: message,
                kind: .card,
                size: .lookout,
                timeType: .linger,
                placement: .upperRight,
                source: "camera",
                ttl: 24,
                layout: "visor",
                image: image
            ),
            stealFocus: false
        )
    }

    private func present(_ content: PresenceContent, stealFocus: Bool) {
        lastContent = content
        let root = PresenceOverlayView(
            content: content,
            onDismiss: { [weak self] in self?.hide(id: content.id) }
        )
        let view = ClearHostingView(rootView: root)
        let size = content.size.panelSize
        view.frame = NSRect(origin: .zero, size: size)
        view.wantsLayer = true
        view.layer?.cornerRadius = 0
        view.layer?.masksToBounds = false
        view.layer?.backgroundColor = NSColor.clear.cgColor
        view.layer?.isOpaque = false

        if var existing = slots[content.id] {
            existing.dismissWork?.cancel()
            existing.content = content
            existing.hosting = view
            existing.panel.contentView = view
            existing.panel.setContentSize(size)
            position(existing.panel, placement: content.placement, size: size, content: content)
            existing.dismissWork = scheduleDismiss(id: content.id, ttl: content.resolvedTTL)
            slots[content.id] = existing
            reveal(existing.panel, stealFocus: stealFocus)
            return
        }

        let panel = makePanel(size: size)
        panel.contentView = view
        panel.setContentSize(size)
        position(panel, placement: content.placement, size: size, content: content)
        let work = scheduleDismiss(id: content.id, ttl: content.resolvedTTL)
        slots[content.id] = Slot(content: content, panel: panel, hosting: view, dismissWork: work)
        reveal(panel, stealFocus: stealFocus)
    }

    private func reveal(_ panel: NSPanel, stealFocus: Bool) {
        if stealFocus {
            NSApp.activate(ignoringOtherApps: true)
            panel.makeKeyAndOrderFront(nil)
        }
        panel.orderFrontRegardless()
    }

    func showLast() {
        if slots.isEmpty, let lastContent {
            show(lastContent)
            return
        }
        for slot in slots.values {
            slot.panel.makeKeyAndOrderFront(nil)
        }
    }

    func hide(id: String? = nil) {
        if let id {
            if let slot = slots.removeValue(forKey: id) {
                slot.dismissWork?.cancel()
                slot.panel.orderOut(nil)
            }
            return
        }
        hideAll()
    }

    func hideAll() {
        for slot in slots.values {
            slot.dismissWork?.cancel()
            slot.panel.orderOut(nil)
        }
        slots.removeAll()
        stackIndex = 0
    }

    private func scheduleDismiss(id: String, ttl: TimeInterval?) -> DispatchWorkItem? {
        guard let ttl, ttl > 0 else { return nil }
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor in
                self?.hide(id: id)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + ttl, execute: work)
        return work
    }

    private func makePanel(size: NSSize) -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.alphaValue = EVPalette.windowOpacity
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.animationBehavior = .utilityWindow
        return panel
    }

    private func position(_ panel: NSPanel, placement: PresencePlacement, size: NSSize, content: PresenceContent? = nil) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let pad: CGFloat = 28
        var origin: NSPoint
        switch placement {
        case .center:
            origin = NSPoint(
                x: visible.midX - size.width / 2,
                y: visible.midY + visible.height * 0.08
            )
        case .upperRight:
            origin = NSPoint(x: visible.maxX - size.width - pad, y: visible.maxY - size.height - pad)
        case .upperLeft:
            origin = NSPoint(x: visible.minX + pad, y: visible.maxY - size.height - pad)
        case .lowerRight:
            origin = NSPoint(x: visible.maxX - size.width - pad, y: visible.minY + pad)
        case .lowerLeft:
            origin = NSPoint(x: visible.minX + pad, y: visible.minY + pad)
        case .right:
            origin = NSPoint(x: visible.maxX - size.width - pad, y: visible.midY - size.height / 2)
        case .left:
            origin = NSPoint(x: visible.minX + pad, y: visible.midY - size.height / 2)
        case .top:
            origin = NSPoint(x: visible.midX - size.width / 2, y: visible.maxY - size.height - 16)
        case .stack:
            let offset = CGFloat(stackIndex % 5) * 28
            stackIndex += 1
            origin = NSPoint(
                x: visible.midX - size.width / 2 + offset,
                y: visible.midY + visible.height * 0.08 - offset
            )
        }
        let dx = content?.driftX ?? 0
        let dy = content?.driftY ?? 0
        origin.x += dx
        origin.y += dy
        panel.setFrameOrigin(origin)
    }
}

// MARK: - View

struct PresenceOverlayView: View {
    let content: PresenceContent
    let onDismiss: () -> Void

    /// Single paragraph only: response ?? message, flattened to one paragraph.
    private var paragraph: String {
        let raw = content.resolvedReply.trimmingCharacters(in: .whitespacesAndNewlines)
        let flat = raw
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        return flat.isEmpty ? content.title.trimmingCharacters(in: .whitespacesAndNewlines) : flat
    }

    var body: some View {
        Text(paragraph)
            .font(.system(size: 14))
            .foregroundStyle(EVPalette.ink)
            .multilineTextAlignment(.leading)
            .fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
            .padding(EdgeInsets(top: 14, leading: 18, bottom: 14, trailing: 18))
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(EVPalette.paneFill)
            )
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .onTapGesture(perform: onDismiss)
    }
}

