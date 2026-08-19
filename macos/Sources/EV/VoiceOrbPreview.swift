import AppKit
import EVRuntime
import ImageIO

/// Temporary visual harness: `swift run --package-path macos EV -- --orb-preview`
///
/// Draws checker / white / color / text behind a real transparent orb panel
/// and writes screenshots. Not used by the shipped menu-bar app.
@MainActor
final class VoiceOrbPreview: NSObject {
    static let shared = VoiceOrbPreview()

    private var background: NSWindow?
    private var renderer: VoiceOrbRenderer?
    private var orbPanel: NSPanel?
    private var step = 0

    func start() {
        let frame = NSRect(x: 80, y: 80, width: 900, height: 640)
        let window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "EV orb transparency test"
        window.isReleasedWhenClosed = false
        let host = PreviewBackgroundView(frame: NSRect(origin: .zero, size: frame.size))
        host.mode = .checker
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        background = window

        let orbSize = VoicePresenceMath.panelSize(visibleWidth: 1440)
        let orbRect = NSRect(
            x: frame.maxX - orbSize.width - 24,
            y: frame.maxY - orbSize.height - 48,
            width: orbSize.width,
            height: orbSize.height
        )
        guard let renderer = VoiceOrbRenderer(frame: NSRect(origin: .zero, size: orbRect.size)) else {
            NSLog("EV orb preview: Metal unavailable")
            return
        }
        self.renderer = renderer
        renderer.setState(status: .speaking, audioLevel: 0, reduceMotion: false)

        let panel = NSPanel(
            contentRect: orbRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = NSColor(calibratedWhite: 0, alpha: 0)
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.level = .floating
        let orbHost = VoiceOrbHostView(frame: NSRect(origin: .zero, size: orbRect.size))
        renderer.view.frame = orbHost.bounds
        renderer.view.autoresizingMask = [.width, .height]
        orbHost.addSubview(renderer.view)
        panel.contentView = orbHost
        panel.orderFrontRegardless()
        orbPanel = panel

        Timer.scheduledTimer(withTimeInterval: 1.4, repeats: true) { [weak self] timer in
            Task { @MainActor in
                self?.captureAndAdvance(timer: timer)
            }
        }
        renderer.seek(seconds: 0)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func captureAndAdvance(timer: Timer) {
        guard let background, let host = background.contentView as? PreviewBackgroundView,
              let renderer else {
            timer.invalidate()
            return
        }
        let stamps: [(String, PreviewBackgroundView.Mode, Double)] = [
            ("checker", .checker, 0),
            ("white", .white, 5),
            ("color", .color, 10),
            ("text", .text, 12.5),
        ]
        guard step < stamps.count else {
            timer.invalidate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                NSApp.terminate(nil)
            }
            return
        }
        let (name, mode, time) = stamps[step]
        host.mode = mode
        renderer.seek(seconds: time)
        host.needsDisplay = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            let rect = background.frame
            let screenRect = NSRect(
                x: rect.origin.x,
                y: rect.origin.y,
                width: rect.width,
                height: rect.height
            )
            let cgRect = CGRect(
                x: screenRect.origin.x,
                y: screenRect.origin.y,
                width: screenRect.width,
                height: screenRect.height
            )
            if let image = CGWindowListCreateImage(
                cgRect,
                [.optionOnScreenOnly],
                kCGNullWindowID,
                [.bestResolution]
            ) {
                let url = URL(fileURLWithPath: "/tmp/ev-orb-preview-\(name).png")
                if let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) {
                    CGImageDestinationAddImage(dest, image, nil)
                    CGImageDestinationFinalize(dest)
                    NSLog("EV orb preview: wrote %@", url.path)
                }
            }
        }
        step += 1
        if step > 3 {
            timer.invalidate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                NSApp.terminate(nil)
            }
        }
    }
}

final class PreviewBackgroundView: NSView {
    enum Mode {
        case checker, white, color, text
    }

    var mode: Mode = .checker

    override var isOpaque: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        switch mode {
        case .white:
            NSColor.white.setFill()
            bounds.fill()
        case .color:
            let colors = [NSColor.red, NSColor.green, NSColor.blue, NSColor.magenta, NSColor.orange]
            let slice = bounds.width / CGFloat(colors.count)
            for (index, color) in colors.enumerated() {
                color.setFill()
                NSRect(x: CGFloat(index) * slice, y: 0, width: slice, height: bounds.height).fill()
            }
        case .text:
            NSColor(calibratedWhite: 0.92, alpha: 1).setFill()
            bounds.fill()
            let text = "BACKGROUND UI TEXT\ncalendar 9:30\nreadable through open regions\nchecker is not this mode"
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 36, weight: .semibold),
                .foregroundColor: NSColor.black,
            ]
            (text as NSString).draw(in: bounds.insetBy(dx: 40, dy: 80), withAttributes: attrs)
        case .checker:
            let cell: CGFloat = 28
            var y: CGFloat = 0
            var row = 0
            while y < bounds.height {
                var x: CGFloat = 0
                var col = 0
                while x < bounds.width {
                    ((row + col) % 2 == 0 ? NSColor(calibratedWhite: 0.95, alpha: 1) : NSColor(calibratedWhite: 0.22, alpha: 1)).setFill()
                    NSRect(x: x, y: y, width: cell, height: cell).fill()
                    x += cell
                    col += 1
                }
                y += cell
                row += 1
            }
        }
    }
}
