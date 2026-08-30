import AppKit
import Combine
import EVRuntime

/// Speech-response orb for EV.app.
///
/// Parked under the menu-bar clock on the top-right of the desktop.
/// Click-through so it never steals the clock or menu extras.
///
/// The visual is the bundled reference MP4, keyed to per-pixel alpha in
/// Metal and shown as a CGImage so the desktop shows through empty regions.
/// A CAMetalLayer or WKWebView canvas paints an opaque rectangle on macOS.
///
/// Accessory (`LSUIElement`) apps discard windows created before
/// `applicationDidFinishLaunching` and `setActivationPolicy(.accessory)`.
/// Attach the model as soon as it exists; only order the panel on screen
/// after launch is ready and speech status wants it visible.
@MainActor
final class VoiceOrbOverlay: NSObject {
    static let shared = VoiceOrbOverlay()

    private var panel: NSPanel?
    private var hostView: VoiceOrbHostView?
    private var renderer: VoiceOrbRenderer?
    private var stillView: VoiceOrbStillView?
    private var model: AppModel?
    private var launchReady = false
    private var pump: Timer?
    private var observers: [NSObjectProtocol] = []
    private var targetDisplayID: CGDirectDisplayID?
    private var revealGeneration = 0
    private var lastSpeech: VoiceOrbSpeechStatus = .hidden
    private var hideWork: DispatchWorkItem?
    private var statusObserver: AnyCancellable?

    private static let screenNumberKey = NSDeviceDescriptionKey("NSScreenNumber")

    private override init() {
        super.init()
    }

    func attach(_ model: AppModel) {
        self.model = model
        if panel == nil {
            build()
        }
        observeStatus(model)
        if launchReady {
            startPump()
            pushState()
        }
        if VoiceOrbDebug.forceVisible {
            lastSpeech = .speaking
            reveal()
        }
    }

    func noteAppDidFinishLaunching() {
        launchReady = true
        if model != nil || VoiceOrbDebug.forceVisible {
            if panel == nil {
                build()
            }
            startPump()
            pushState()
        }
        if VoiceOrbDebug.forceVisible {
            lastSpeech = .speaking
            reveal()
        }
    }

    func show() {
        guard launchReady else { return }
        if lastSpeech == .preparing || lastSpeech == .speaking {
            reveal()
        } else {
            reposition()
        }
    }

    func hide() {
        hideWork?.cancel()
        hideWork = nil
        lastSpeech = .hidden
        revealGeneration += 1
        pump?.invalidate()
        pump = nil
        renderer?.setState(status: .hidden, audioLevel: 0, reduceMotion: false)
        renderer?.pause()
        panel?.orderOut(nil)
    }

    private func reveal() {
        guard launchReady, let panel else { return }
        hideWork?.cancel()
        hideWork = nil
        revealGeneration += 1
        let generation = revealGeneration
        reposition()
        makePanelTransparent(panel)
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        startPump()
        renderer?.resume()
        NSLog(
            "[ORB] REVEAL speech=%@ panelVisible=%d frame=%@",
            lastSpeech.rawValue,
            panel.isVisible ? 1 : 0,
            NSStringFromRect(panel.frame)
        )
        DispatchQueue.main.async { [weak self] in
            guard let self, self.revealGeneration == generation,
                  let panel = self.panel else { return }
            self.reposition()
            self.makePanelTransparent(panel)
            panel.orderFrontRegardless()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self, self.revealGeneration == generation,
                  let panel = self.panel else { return }
            self.reposition()
            self.makePanelTransparent(panel)
            panel.orderFrontRegardless()
        }
    }

    private func scheduleHide() {
        hideWork?.cancel()
        let generation = revealGeneration
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.revealGeneration == generation else { return }
            guard self.lastSpeech == .hidden || self.lastSpeech == .ending else { return }
            self.pump?.invalidate()
            self.pump = nil
            self.renderer?.pause()
            self.panel?.orderOut(nil)
        }
        hideWork = work
        DispatchQueue.main.asyncAfter(
            deadline: .now() + VoicePresenceMath.fadeDuration + 0.06,
            execute: work
        )
    }

    private func build() {
        let size = defaultSize()
        let host = VoiceOrbHostView(frame: NSRect(origin: .zero, size: size))
        host.wantsLayer = false
        host.setAccessibilityElement(false)

        if let renderer = VoiceOrbRenderer(frame: host.bounds) {
            renderer.view.autoresizingMask = [.width, .height]
            renderer.view.setAccessibilityElement(false)
            host.addSubview(renderer.view)
            self.renderer = renderer
            NSLog("[ORB] CREATE VoiceOrbRenderer attached to overlay")
        } else {
            let still = VoiceOrbStillView(image: nil, frame: host.bounds)
            still.autoresizingMask = [.width, .height]
            still.setAccessibilityElement(false)
            host.addSubview(still)
            self.stillView = still
            NSLog("[ORB] CREATE VoiceOrbStillView FALLBACK — Metal renderer unavailable")
        }
        if VoiceOrbDebug.showsMarker {
            let marker = NSTextField(labelWithString: "ORB DEBUG \(VoiceOrbIdentity.buildID)")
            marker.font = NSFont.monospacedSystemFont(ofSize: 9, weight: .bold)
            marker.textColor = NSColor.systemYellow
            marker.backgroundColor = NSColor.black.withAlphaComponent(0.55)
            marker.drawsBackground = true
            marker.isBordered = false
            marker.isBezeled = false
            marker.alignment = .left
            marker.frame = NSRect(x: 4, y: 4, width: max(120, size.width - 8), height: 16)
            marker.setAccessibilityElement(false)
            host.addSubview(marker)
        }

        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = NSWindow.Level.statusBar
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .ignoresCycle,
            .stationary,
        ]
        panel.isMovableByWindowBackground = false
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none
        panel.hasShadow = false
        panel.setAccessibilityElement(false)
        panel.contentView = host
        panel.setContentSize(size)
        makePanelTransparent(panel)
        self.panel = panel
        self.hostView = host
        VoiceOrbIdentity.report(
            component: "VoiceOrbOverlay",
            pointer: String(describing: ObjectIdentifier(self)),
            extra: [
                "panelOpaque": String(panel.isOpaque),
                "panelAlpha": String(format: "%.3f", Double(panel.alphaValue)),
                "hostOpaque": String(host.isOpaque),
                "hostWantsLayer": String(host.wantsLayer),
                "rendererPresent": renderer == nil ? "false" : "true",
                "fallbackPresent": stillView == nil ? "false" : "true",
                "childCount": String(host.subviews.count),
            ]
        )
        installObservers()
        reposition()
        logComposition("build")
    }

    private func makePanelTransparent(_ panel: NSPanel) {
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.alphaValue = 1
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        if let host = panel.contentView {
            host.wantsLayer = false
        }
        renderer?.view.applyTransparentLayer()
    }

    private var loggedComposition = Set<String>()

    private func logComposition(_ reason: String) {
        if loggedComposition.contains(reason) { return }
        loggedComposition.insert(reason)
        guard let panel else { return }
        let host = panel.contentView
        NSLog(
            "[ORB] COMPOSITION reason=%@ isOpaque=%d bg=%@ alpha=%.3f hostOpaque=%d hostLayerOpaque=%@ viewLayerOpaque=%@ subviews=%ld frame=%@",
            reason,
            panel.isOpaque ? 1 : 0,
            panel.backgroundColor?.description ?? "nil",
            panel.alphaValue,
            host?.isOpaque == true ? 1 : 0,
            host?.layer.map { String($0.isOpaque) } ?? "no-layer",
            renderer?.view.layer.map { String($0.isOpaque) } ?? "no-layer",
            host?.subviews.count ?? 0,
            NSStringFromRect(panel.frame)
        )
        host?.subviews.enumerated().forEach { index, child in
            NSLog("[ORB] CHILD %ld %@ frame=%@", index, String(describing: type(of: child)), NSStringFromRect(child.frame))
        }
    }

    private func observeStatus(_ model: AppModel) {
        statusObserver = model.$status
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.pushState()
                }
            }
    }

    private func startPump() {
        guard pump == nil else { return }
        let timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
            Task { @MainActor in
                VoiceOrbOverlay.shared.pushState()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        pump = timer
    }

    private func pushState() {
        if VoiceOrbDebug.forceVisible {
            applySpeech(.speaking)
            renderer?.setState(status: .speaking, audioLevel: 0, reduceMotion: false)
            stillView?.isHidden = renderer != nil
            stillView?.alphaValue = renderer == nil ? 1 : 0
            logComposition("force")
            return
        }
        guard let model else { return }
        let levels = VoiceLevelMeter.shared.snapshot()
        let energy = VoiceOrbEnergy.resolve(
            status: model.status,
            muted: model.isLiveMuted,
            live: model.isLiveActive,
            input: levels.input,
            output: levels.output,
            time: Date().timeIntervalSinceReferenceDate
        )
        applySpeech(energy.status)
        let reduce = UserDefaults(suiteName: "com.apple.universalaccess")?
            .bool(forKey: "reduceMotion") ?? false
        renderer?.setState(
            status: energy.status,
            audioLevel: energy.audioLevel,
            reduceMotion: reduce
        )
        if stillView != nil {
            stillView?.isHidden = !(energy.status == .preparing || energy.status == .speaking)
            stillView?.alphaValue = (energy.status == .preparing || energy.status == .speaking) ? 1 : 0
        }
    }

    private func applySpeech(_ speech: VoiceOrbSpeechStatus) {
        let previous = lastSpeech
        lastSpeech = speech
        guard launchReady else { return }
        switch speech {
        case .preparing, .speaking:
            if previous == .hidden || previous == .ending || panel?.isVisible != true {
                reveal()
            }
        case .hidden, .ending:
            if previous == .preparing || previous == .speaking {
                scheduleHide()
            }
        }
    }

    private func installObservers() {
        guard observers.isEmpty else { return }
        let center = NotificationCenter.default
        observers.append(
            center.addObserver(
                forName: NSApplication.didChangeScreenParametersNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    self?.reposition()
                    self?.show()
                }
            }
        )
        observers.append(
            NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.activeSpaceDidChangeNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    self?.show()
                }
            }
        )
    }

    private func defaultSize() -> NSSize {
        let visibleWidth = NSScreen.screens.first.map { Double($0.visibleFrame.width) } ?? 1440
        let size = VoicePresenceMath.panelSize(visibleWidth: visibleWidth)
        return NSSize(width: size.width, height: size.height)
    }

    private func reposition() {
        guard let panel else { return }
        guard let screen = targetScreen() else { return }
        let visible = screen.visibleFrame
        guard visible.width > 0, visible.height > 0 else { return }
        let size = VoicePresenceMath.panelSize(visibleWidth: Double(visible.width))
        let origin = VoicePresenceMath.overlayOrigin(
            visibleX: Double(visible.minX),
            visibleY: Double(visible.minY),
            visibleWidth: Double(visible.width),
            visibleHeight: Double(visible.height),
            sizeWidth: size.width,
            sizeHeight: size.height
        )
        panel.setFrame(
            NSRect(
                x: origin.x,
                y: origin.y,
                width: size.width,
                height: size.height
            ),
            display: true
        )
        makePanelTransparent(panel)
        renderer?.view.applyTransparentLayer()
    }

    /// The orb belongs to the MacBook panel, not whichever display happens to
    /// own the current key window. `NSScreen.main` changes with app focus and
    /// can move the orb to an external display after a Space switch.
    private func targetScreen() -> NSScreen? {
        let screens = NSScreen.screens

        if let builtIn = screens.first(where: { screen in
            guard let displayID = displayID(for: screen) else { return false }
            return CGDisplayIsBuiltin(displayID) != 0
        }) {
            targetDisplayID = displayID(for: builtIn)
            return builtIn
        }

        if let targetDisplayID,
           let previous = screens.first(where: { displayID(for: $0) == targetDisplayID }) {
            return previous
        }

        return panel?.screen ?? NSScreen.main ?? screens.first
    }

    private func displayID(for screen: NSScreen) -> CGDirectDisplayID? {
        guard let number = screen.deviceDescription[Self.screenNumberKey] as? NSNumber else {
            return nil
        }
        return CGDirectDisplayID(number.uint32Value)
    }
}
