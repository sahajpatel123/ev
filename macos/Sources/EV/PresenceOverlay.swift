import AppKit
import MapKit
import SwiftUI

// MARK: - Visual language

enum EVPalette {
    /// Whole window sits at 75% opacity.
    static let windowOpacity: CGFloat = 0.75
    /// Pane tint: 30% opaque, so 70% of the desktop shows through the glass.
    static let paneFill = Color(red: 0.055, green: 0.043, blue: 0.035).opacity(0.30)
    static let ink = Color(red: 0.957, green: 0.937, blue: 0.902)
    static let ask = Color(red: 0.941, green: 0.851, blue: 0.722)
    static let reply = Color(red: 0.863, green: 0.910, blue: 0.875)
    static let accent = Color(red: 0.773, green: 0.416, blue: 0.235)
    static let muted = Color.white.opacity(0.46)
    static let rule = Color.white.opacity(0.14)
}

enum PresenceLayout: String, CaseIterable {
    case ask, reply, split, stack, pulse, ribbon, field, ledger, visor
}

func stableInt(_ key: String) -> UInt32 {
    var value: UInt32 = 5381
    for byte in key.utf8 {
        value = value &* 33 &+ UInt32(byte)
    }
    return value
}

func pickLayout(for content: PresenceContent) -> PresenceLayout {
    if content.image != nil {
        return .visor
    }
    if let raw = content.layout, let layout = PresenceLayout(rawValue: raw) {
        return layout
    }
    switch content.kind {
    case .ticker, .conversation: return .ribbon
    case .chip, .pulse: return .pulse
    case .map: return .field
    default: break
    }
    let asks = content.questions.filter { !$0.isEmpty }
    let reply = (content.response ?? content.message).trimmingCharacters(in: .whitespacesAndNewlines)
    let seed = Int(stableInt(content.id))
    let pool: [PresenceLayout]
    if !asks.isEmpty && !reply.isEmpty {
        pool = [.ask, .reply, .split, .ledger, .stack]
    } else if !asks.isEmpty {
        pool = [.ask, .stack, .ledger]
    } else if !content.items.isEmpty {
        pool = [.stack, .field, .ledger]
    } else {
        pool = [.reply, .stack]
    }
    return pool[seed % pool.count]
}

func pickDrift(id: String, placement: PresencePlacement) -> (CGFloat, CGFloat, Double) {
    let seed = Int(stableInt(id))
    if placement == .top {
        return (CGFloat((seed % 41) - 20), 0, 0)
    }
    if placement == .center {
        return (
            CGFloat((seed % 49) - 24),
            CGFloat(((seed >> 8) % 37) - 18),
            Double(((seed >> 14) % 29) - 14) / 16.0
        )
    }
    return (
        CGFloat((seed % 73) - 36),
        CGFloat(((seed >> 7) % 61) - 30),
        Double(((seed >> 14) % 29) - 14) / 10.0
    )
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
    var resolvedLayout: PresenceLayout { pickLayout(for: self) }
    var resolvedReply: String {
        let reply = (response ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return reply.isEmpty ? message : reply
    }
}

// MARK: - Controller

/// EVIE's native HUD — she opens as many folios as intelligence asked for.
/// Translucent sheets with a spine, no title bar. Dismiss with Escape,
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

    func show(_ content: PresenceContent) {
        present(content, stealFocus: true)
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
        let fallback = pickDrift(id: content?.id ?? "", placement: placement)
        let dx = content?.driftX ?? fallback.0
        let dy = content?.driftY ?? fallback.1
        origin.x += dx
        origin.y += dy
        panel.setFrameOrigin(origin)
    }
}

// MARK: - View

struct PresenceOverlayView: View {
    let content: PresenceContent
    let onDismiss: () -> Void
    @State private var pulse = false

    private var folioShape: UnevenRoundedRectangle {
        UnevenRoundedRectangle(
            topLeadingRadius: 2,
            bottomLeadingRadius: 2,
            bottomTrailingRadius: 16,
            topTrailingRadius: 16,
            style: .continuous
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: content.resolvedLayout == .ribbon || content.resolvedLayout == .pulse ? 6 : 12) {
            header
            if content.kind == .map {
                MapBody(content: content)
            } else {
                FolioBody(content: content)
            }
        }
        .padding(content.kind == .ticker ? EdgeInsets(top: 12, leading: 18, bottom: 12, trailing: 16) : EdgeInsets(top: 18, leading: 22, bottom: 20, trailing: 18))
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            folioShape
                .fill(EVPalette.paneFill)
                .overlay(alignment: .leading) {
                    Rectangle()
                        .fill(EVPalette.accent)
                        .frame(width: 4)
                }
        )
        .clipShape(folioShape)
        .shadow(color: .black.opacity(0.22), radius: 22, y: 10)
        .rotationEffect(.degrees(content.tilt ?? pickDrift(id: content.id, placement: content.placement).2))
        .onAppear {
            if content.lookout || content.timeType == .pulse || content.timeType == .lookout {
                withAnimation(.easeInOut(duration: 1.8).repeatForever()) {
                    pulse = true
                }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("EVIE")
                .font(.system(size: 10, weight: .semibold))
                .tracking(1.6)
                .foregroundStyle(EVPalette.accent)
            if content.lookout || content.timeType == .lookout || content.timeType == .pulse {
                RoundedRectangle(cornerRadius: 1)
                    .fill(EVPalette.accent)
                    .frame(width: 7, height: 7)
                    .opacity(pulse ? 1 : 0.25)
            }
            Text(content.resolvedLayout.rawValue)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(EVPalette.muted)
            if let source = content.source, !source.isEmpty {
                Text(source)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(EVPalette.muted)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            Button("dismiss", action: onDismiss)
                .buttonStyle(.plain)
                .font(.system(size: 11))
                .foregroundStyle(EVPalette.muted)
                .keyboardShortcut(.escape, modifiers: [])
        }
    }
}

private struct FolioBody: View {
    let content: PresenceContent

    var body: some View {
        switch content.resolvedLayout {
        case .ask:
            VStack(alignment: .leading, spacing: 12) {
                AskBlock(questions: asks)
                if !content.resolvedReply.isEmpty {
                    ReplyBlock(text: content.resolvedReply)
                }
                NotesBlock(items: content.items, field: false)
                SteerBlock(text: content.recommendation)
                Spacer(minLength: 0)
            }
        case .reply:
            VStack(alignment: .leading, spacing: 12) {
                Text(content.title)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(EVPalette.ink)
                ReplyBlock(text: content.resolvedReply)
                AskBlock(questions: asks)
                NotesBlock(items: content.items, field: false)
                SteerBlock(text: content.recommendation)
                Spacer(minLength: 0)
            }
        case .split:
            HStack(alignment: .top, spacing: 16) {
                AskBlock(questions: asks)
                    .frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 10) {
                    ReplyBlock(text: content.resolvedReply)
                    NotesBlock(items: content.items, field: false)
                    SteerBlock(text: content.recommendation)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(alignment: .leading) {
                    Rectangle().fill(EVPalette.rule).frame(width: 1)
                }
                .padding(.leading, 12)
            }
        case .ledger:
            VStack(alignment: .leading, spacing: 14) {
                ForEach(Array(asks.enumerated()), id: \.offset) { index, question in
                    VStack(alignment: .leading, spacing: 6) {
                        AskBlock(questions: [question])
                        if index == 0 {
                            ReplyBlock(text: content.resolvedReply)
                        } else if content.items.indices.contains(index - 1) {
                            ReplyBlock(text: content.items[index - 1])
                        }
                    }
                    .padding(.bottom, 8)
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(EVPalette.rule).frame(height: 1)
                    }
                }
                SteerBlock(text: content.recommendation)
                Spacer(minLength: 0)
            }
        case .field:
            VStack(alignment: .leading, spacing: 10) {
                Text(content.title)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(EVPalette.ink)
                Text(content.message)
                    .font(.system(size: 14))
                    .foregroundStyle(EVPalette.ink.opacity(0.78))
                NotesBlock(items: content.items, field: true)
                SteerBlock(text: content.recommendation)
                Spacer(minLength: 0)
            }
        case .ribbon, .pulse:
            Group {
                if content.resolvedLayout == .ribbon {
                    HStack(alignment: .firstTextBaseline, spacing: 16) {
                        Text(asks.first ?? content.title)
                            .font(.system(size: 16, design: .serif))
                            .italic()
                            .foregroundStyle(EVPalette.ask)
                        Text(content.resolvedReply)
                            .font(.system(size: 14))
                            .foregroundStyle(EVPalette.reply)
                            .lineLimit(2)
                    }
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(asks.first ?? content.title)
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(EVPalette.ink)
                            .lineLimit(1)
                        Text(content.resolvedReply.replacingOccurrences(of: "+", with: " "))
                            .font(.system(size: 13))
                            .foregroundStyle(EVPalette.reply)
                            .lineLimit(4)
                    }
                }
            }
        case .stack:
            VStack(alignment: .leading, spacing: 10) {
                Text(content.title)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(EVPalette.ink)
                AskBlock(questions: asks)
                ReplyBlock(text: content.resolvedReply)
                NotesBlock(items: content.items, field: false)
                SteerBlock(text: content.recommendation)
                Spacer(minLength: 0)
            }
        case .visor:
            VStack(alignment: .leading, spacing: 10) {
                Text(content.title)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(EVPalette.ink)
                if let image = content.image {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                if !content.message.isEmpty {
                    Text(content.message)
                        .font(.system(size: 12))
                        .foregroundStyle(EVPalette.muted)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var asks: [String] {
        let listed = content.questions.filter { !$0.isEmpty }
        if !listed.isEmpty { return listed }
        if content.message.contains("?") {
            return content.message
                .split(separator: "?")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) + "?" }
                .filter { $0.count > 8 }
        }
        return []
    }
}

private struct AskBlock: View {
    let questions: [String]

    var body: some View {
        if !questions.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(questions, id: \.self) { question in
                    VStack(alignment: .leading, spacing: 4) {
                        Text("ask")
                            .font(.system(size: 10, weight: .semibold))
                            .tracking(1.4)
                            .textCase(.uppercase)
                            .foregroundStyle(EVPalette.accent)
                        Text(question)
                            .font(.system(size: 20, design: .serif))
                            .italic()
                            .foregroundStyle(EVPalette.ask)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }
}

private struct ReplyBlock: View {
    let text: String

    var body: some View {
        if !text.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("evie")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(1.4)
                    .textCase(.uppercase)
                    .foregroundStyle(EVPalette.accent)
                Text(text)
                    .font(.system(size: 15))
                    .foregroundStyle(EVPalette.reply)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
    }
}

private struct NotesBlock: View {
    let items: [String]
    let field: Bool

    var body: some View {
        if !items.isEmpty {
            if field {
                FlexibleNotes(items: items)
            } else {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(items, id: \.self) { item in
                        HStack(alignment: .top, spacing: 8) {
                            Rectangle()
                                .fill(EVPalette.accent)
                                .frame(width: 8, height: 1)
                                .padding(.top, 8)
                            Text(item)
                                .font(.system(size: 13))
                                .foregroundStyle(EVPalette.ink.opacity(0.78))
                                .textSelection(.enabled)
                        }
                    }
                }
            }
        }
    }
}

private struct FlexibleNotes: View {
    let items: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(items, id: \.self) { item in
                Text(item)
                    .font(.system(size: 13))
                    .foregroundStyle(EVPalette.ink.opacity(0.78))
                    .padding(.bottom, 4)
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(EVPalette.rule).frame(height: 1)
                    }
            }
        }
    }
}

private struct SteerBlock: View {
    let text: String?

    var body: some View {
        if let text, !text.isEmpty {
            Text(text)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(EVPalette.ink)
                .padding(.top, 8)
                .overlay(alignment: .top) {
                    Rectangle().fill(EVPalette.rule).frame(height: 1)
                }
        }
    }
}

private struct MapBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(EVPalette.ink)
            if let origin = content.origin, let destination = content.destination {
                PresenceMapView(origin: origin, destination: destination)
                    .frame(maxHeight: .infinity)
            } else {
                VStack(spacing: 10) {
                    Text("A live map appears when route coordinates are supplied.")
                        .font(.system(size: 13))
                        .foregroundStyle(EVPalette.muted)
                        .multilineTextAlignment(.center)
                    if !content.message.isEmpty {
                        Text(content.message)
                            .font(.system(size: 12))
                            .foregroundStyle(EVPalette.muted)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}

private struct PresenceMapView: View {
    let origin: CLLocationCoordinate2D
    let destination: CLLocationCoordinate2D
    @State private var position: MapCameraPosition

    init(origin: CLLocationCoordinate2D, destination: CLLocationCoordinate2D) {
        self.origin = origin
        self.destination = destination
        let center = CLLocationCoordinate2D(
            latitude: (origin.latitude + destination.latitude) / 2,
            longitude: (origin.longitude + destination.longitude) / 2
        )
        _position = State(initialValue: .region(MKCoordinateRegion(
            center: center,
            latitudinalMeters: 6000,
            longitudinalMeters: 6000
        )))
    }

    var body: some View {
        Map(position: $position) {
            Marker("EVIE", coordinate: origin)
                .tint(EVPalette.accent)
            Marker("Destination", coordinate: destination)
                .tint(EVPalette.ask)
        }
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

