import AppKit
import MapKit
import SwiftUI

// MARK: - Visual language

enum EVPalette {
    static let cyan = Color(red: 0.45, green: 0.92, blue: 1.0)
    static let gold = Color(red: 0.93, green: 0.78, blue: 0.42)
    /// Whole window sits at 75% opacity.
    static let windowOpacity: CGFloat = 0.75
    /// Pane tint: 30% opaque, so 70% of the desktop shows through the glass.
    static let paneFill = Color(red: 0.04, green: 0.05, blue: 0.08).opacity(0.30)
    static let frost = Color.white.opacity(0.08)
    static let glass = Color.white.opacity(0.06)
    static let stroke = LinearGradient(
        colors: [cyan.opacity(0.75), gold.opacity(0.35)],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )
}

// MARK: - Catalogs (JARVIS sizes / Karen time-types / lookouts)

enum PresenceKind: String, CaseIterable {
    case card, briefing, list, conversation, map
    case chip, radar, vitals, horizon, scope, bench, trace, pulse, ticker, wire

    var label: String { rawValue.uppercased() }

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
    var recommendation: String?
    var source: String?
    var lookout: Bool = false
    var ttl: TimeInterval?
    var origin: CLLocationCoordinate2D?
    var destination: CLLocationCoordinate2D?

    var resolvedTTL: TimeInterval? { ttl ?? timeType.defaultTTL }
}

// MARK: - Controller

/// EVIE's native HUD — she opens as many windows as intelligence asked for.
/// Dark glass, cyan EVIE + warm gold JARVIS. Dismiss with Escape, close,
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
            layer?.cornerRadius = 20
            layer?.masksToBounds = true
            window?.isOpaque = false
            window?.backgroundColor = .clear
            window?.alphaValue = EVPalette.windowOpacity
            superview?.wantsLayer = true
            superview?.layer?.backgroundColor = NSColor.clear.cgColor
        }
    }

    var lastContents: [PresenceContent] { slots.values.map(\.content) }

    func show(_ content: PresenceContent) {
        lastContent = content
        let root = PresenceOverlayView(
            content: content,
            onDismiss: { [weak self] in self?.hide(id: content.id) }
        )
        let view = ClearHostingView(rootView: root)
        let size = content.size.panelSize
        view.frame = NSRect(origin: .zero, size: size)
        view.wantsLayer = true
        view.layer?.cornerRadius = 20
        view.layer?.masksToBounds = true
        view.layer?.backgroundColor = NSColor.clear.cgColor
        view.layer?.isOpaque = false

        if var existing = slots[content.id] {
            existing.dismissWork?.cancel()
            existing.content = content
            existing.hosting = view
            existing.panel.contentView = view
            existing.panel.setContentSize(size)
            position(existing.panel, placement: content.placement, size: size)
            existing.dismissWork = scheduleDismiss(id: content.id, ttl: content.resolvedTTL)
            slots[content.id] = existing
            existing.panel.makeKeyAndOrderFront(nil)
            existing.panel.orderFrontRegardless()
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let panel = makePanel(size: size)
        panel.contentView = view
        panel.setContentSize(size)
        position(panel, placement: content.placement, size: size)
        let work = scheduleDismiss(id: content.id, ttl: content.resolvedTTL)
        slots[content.id] = Slot(content: content, panel: panel, hosting: view, dismissWork: work)
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
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
            styleMask: [.titled, .closable, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.title = "EVIE"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.alphaValue = EVPalette.windowOpacity
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.animationBehavior = .utilityWindow
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        return panel
    }

    private func position(_ panel: NSPanel, placement: PresencePlacement, size: NSSize) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let pad: CGFloat = 24
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
        panel.setFrameOrigin(origin)
    }
}

// MARK: - View

struct PresenceOverlayView: View {
    let content: PresenceContent
    let onDismiss: () -> Void
    @State private var pulse = false

    var body: some View {
        VStack(alignment: .leading, spacing: content.kind == .ticker || content.kind == .chip ? 6 : 12) {
            header
            contentBody
        }
        .padding(content.kind == .ticker ? 12 : 22)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(EVPalette.paneFill)
                .overlay(EVPalette.frost)
                .overlay(
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .stroke(EVPalette.stroke, lineWidth: 1)
                )
        )
        .shadow(color: EVPalette.cyan.opacity(0.12), radius: 18, y: 6)
        .onAppear {
            if content.lookout || content.timeType == .pulse || content.timeType == .lookout {
                withAnimation(.easeInOut(duration: 1.1).repeatForever()) {
                    pulse = true
                }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("EVIE")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(3)
                .foregroundStyle(EVPalette.cyan)
            if content.lookout || content.timeType == .lookout || content.timeType == .pulse {
                Circle()
                    .fill(EVPalette.cyan)
                    .frame(width: 7, height: 7)
                    .opacity(pulse ? 1 : 0.25)
            }
            if let source = content.source, !source.isEmpty {
                Text(source)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.45))
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            Text(content.timeType.rawValue.uppercased())
                .font(.system(size: 9, weight: .medium, design: .monospaced))
                .foregroundStyle(EVPalette.gold.opacity(0.7))
            Text(content.kind.label)
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(EVPalette.gold.opacity(0.9))
            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.7))
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.escape, modifiers: [])
        }
    }

    @ViewBuilder
    private var contentBody: some View {
        switch content.kind {
        case .card: CardBody(content: content)
        case .briefing: BriefingBody(content: content)
        case .list, .radar, .trace, .bench: ListBody(content: content)
        case .conversation, .wire:
            if content.items.isEmpty {
                ChipBody(content: content)
            } else {
                ConversationBody(content: content)
            }
        case .map: MapBody(content: content)
        case .chip, .pulse, .ticker: ChipBody(content: content)
        case .vitals: VitalsBody(content: content)
        case .horizon, .scope: LookoutBody(content: content)
        }
    }
}

private struct SectionTitle: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .semibold, design: .monospaced))
            .tracking(1.5)
            .foregroundStyle(EVPalette.gold.opacity(0.9))
    }
}

private struct CardBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 22, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            ScrollView {
                Text(content.message)
                    .font(.system(size: 14, design: .rounded))
                    .foregroundStyle(.white.opacity(0.86))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
    }
}

private struct ChipBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(content.title)
                .font(.system(size: 16, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
                .lineLimit(1)
            Text(content.message.replacingOccurrences(of: "+", with: " "))
                .font(.system(size: 13, design: .rounded))
                .foregroundStyle(.white.opacity(0.88))
                .lineLimit(content.kind == .ticker ? 2 : 4)
        }
    }
}

private struct VitalsBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            Text(content.message)
                .font(.system(size: 14, design: .rounded))
                .foregroundStyle(.white.opacity(0.86))
            ForEach(content.items, id: \.self) { item in
                Text(item)
                    .font(.system(size: 12, weight: .medium, design: .monospaced))
                    .foregroundStyle(EVPalette.cyan.opacity(0.9))
            }
            Spacer(minLength: 0)
        }
    }
}

private struct LookoutBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            Text(content.message)
                .font(.system(size: 13, design: .rounded))
                .foregroundStyle(.white.opacity(0.82))
            if !content.items.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(content.items, id: \.self) { item in
                        HStack(alignment: .top, spacing: 8) {
                            Circle()
                                .stroke(EVPalette.cyan, lineWidth: 1)
                                .frame(width: 7, height: 7)
                                .padding(.top, 4)
                            Text(item)
                                .font(.system(size: 12, design: .rounded))
                                .foregroundStyle(.white.opacity(0.82))
                        }
                    }
                }
            }
            Spacer(minLength: 0)
        }
    }
}

private struct BriefingBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 22, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            if !content.message.isEmpty {
                SectionTitle(text: "CONTEXT")
                Text(content.message)
                    .font(.system(size: 13, design: .rounded))
                    .foregroundStyle(.white.opacity(0.82))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let recommendation = content.recommendation, !recommendation.isEmpty {
                SectionTitle(text: "RECOMMENDATION")
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "sparkles")
                        .foregroundStyle(EVPalette.gold)
                    Text(recommendation)
                        .font(.system(size: 13, weight: .medium, design: .rounded))
                        .foregroundStyle(.white)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !content.items.isEmpty {
                SectionTitle(text: "TALKING POINTS")
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(content.items, id: \.self) { item in
                        HStack(alignment: .top, spacing: 8) {
                            Circle()
                                .fill(EVPalette.cyan)
                                .frame(width: 5, height: 5)
                                .padding(.top, 5)
                            Text(item)
                                .font(.system(size: 12, design: .rounded))
                                .foregroundStyle(.white.opacity(0.8))
                        }
                    }
                }
            }
            Spacer(minLength: 0)
        }
    }
}

private struct ListBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 22, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            if !content.message.isEmpty {
                Text(content.message)
                    .font(.system(size: 13, design: .rounded))
                    .foregroundStyle(.white.opacity(0.75))
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(content.items, id: \.self) { item in
                        HStack(alignment: .top, spacing: 9) {
                            Image(systemName: "chevron.right")
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(EVPalette.cyan)
                                .padding(.top, 4)
                            Text(item)
                                .font(.system(size: 13, design: .rounded))
                                .foregroundStyle(.white.opacity(0.86))
                                .textSelection(.enabled)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

private struct ConversationBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            if !content.message.isEmpty {
                ForEach(Self.sentences(content.message), id: \.self) { line in
                    Text(line)
                        .font(.system(size: 15, design: .rounded))
                        .foregroundStyle(.white.opacity(0.9))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !content.items.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(content.items.enumerated()), id: \.offset) { index, item in
                            let isEV = index.isMultiple(of: 2)
                            HStack {
                                if isEV { Spacer(minLength: 40) }
                                Text(item)
                                    .font(.system(size: 13, design: .rounded))
                                    .foregroundStyle(.white.opacity(0.9))
                                    .padding(.horizontal, 12)
                                    .padding(.vertical, 8)
                                    .background(
                                        Capsule()
                                            .fill(isEV ? EVPalette.cyan.opacity(0.16) : EVPalette.gold.opacity(0.14))
                                    )
                                    .overlay(
                                        Capsule()
                                            .stroke(
                                                (isEV ? EVPalette.cyan : EVPalette.gold).opacity(0.35),
                                                lineWidth: 1
                                            )
                                    )
                                if !isEV { Spacer(minLength: 40) }
                            }
                            .frame(maxWidth: .infinity)
                        }
                    }
                }
            }
            Spacer(minLength: 0)
        }
    }

    private static func sentences(_ text: String) -> [String] {
        text
            .replacingOccurrences(of: "+", with: " ")
            .split(whereSeparator: { $0 == "." || $0 == "!" || $0 == "?" })
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .map { part in
                if text.contains(where: { $0 == "." || $0 == "!" || $0 == "?" }) {
                    return part + "."
                }
                return part
            }
    }
}

private struct MapBody: View {
    let content: PresenceContent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(content.title)
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
            if let origin = content.origin, let destination = content.destination {
                PresenceMapView(origin: origin, destination: destination)
                    .frame(maxHeight: .infinity)
            } else {
                VStack(spacing: 10) {
                    Image(systemName: "map")
                        .font(.system(size: 34))
                        .foregroundStyle(EVPalette.cyan.opacity(0.7))
                    Text("EVIE can show a live map when route coordinates are supplied.")
                        .font(.system(size: 13, design: .rounded))
                        .foregroundStyle(.white.opacity(0.7))
                        .multilineTextAlignment(.center)
                    if !content.message.isEmpty {
                        Text(content.message)
                            .font(.system(size: 12, design: .rounded))
                            .foregroundStyle(.white.opacity(0.55))
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
                .tint(EVPalette.cyan)
            Marker("Destination", coordinate: destination)
                .tint(EVPalette.gold)
        }
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(EVPalette.stroke, lineWidth: 1)
        )
    }
}
