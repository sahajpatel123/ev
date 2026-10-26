import AppKit
import Metal
import QuartzCore
import SwiftUI

/// The live orb: an `NSView` hosting a `CAMetalLayer` driven by an
/// `OrbStateMachine` on a `FramePacer` display link. Same shader and policy
/// as the headless path — zero frames when static or invisible.
///
/// Set `state` (from presence events via ``PresenceMapping``) and attach a
/// `levelSource` (typically `AtomicOrbLevelMeter.shared`, fed by the app's
/// existing audio taps). Without Metal (or without a level source) the view
/// degrades honestly: a static ring, never a fake animation.
public final class EvieOrbView: NSView {
    /// Current visual state. Cheap to set from any thread.
    public var state: OrbState {
        get { machine.state }
        set {
            machine.setState(newValue)
            manageLink()
        }
    }

    /// Audio levels, polled once per tick. Nil disables level breathing.
    public var levelSource: OrbLevelSource?

    /// Forced appearance, or nil to follow the effective appearance.
    public var appearanceOverride: OrbAppearance?

    private let machine: OrbStateMachine
    private let pacer = FramePacer()
    private let renderer: OrbRenderer?
    private var metalLayer: CAMetalLayer?
    private var fallbackLayer: CAShapeLayer?
    private let stateLock = NSLock()
    private var layerSizePoints: CGSize = .zero
    private var shaderTime: TimeInterval = 0
    private var lastRendered = OrbParameters()
    private var lastTickAt = Date()

    public init(
        frame: CGRect = CGRect(x: 0, y: 0, width: 40, height: 40),
        reduceMotion: Bool = FramePacer.reduceMotion,
        appearance: OrbAppearance? = nil
    ) {
        let resolvedAppearance = appearance ?? .dark
        machine = OrbStateMachine(reduceMotion: reduceMotion, appearance: resolvedAppearance)
        appearanceOverride = appearance
        renderer = try? OrbRenderer()
        super.init(frame: frame)
        wantsLayer = true
        layerSizePoints = bounds.size
        if let renderer {
            let layer = CAMetalLayer()
            layer.device = renderer.metalDevice
            layer.pixelFormat = .bgra8Unorm
            layer.framebufferOnly = true
            layer.isOpaque = false
            layer.contentsScale = window?.backingScaleFactor ?? 2
            self.layer = layer
            metalLayer = layer
        } else {
            installFallbackRing()
        }
        pacer.onTick = { [weak self] _ in self?.tick() }
        manageLink()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("EvieOrbView does not support NSCoding")
    }

    deinit {
        pacer.stop()
    }

    /// Mirror of the host panel's visibility (occlusion, Spaces, sleep).
    /// False stops all ticks; the last frame stays cached in the layer.
    public var isVisible: Bool {
        get { pacer.isVisible }
        set {
            pacer.isVisible = newValue
            manageLink()
        }
    }

    public override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        stateLock.lock()
        layerSizePoints = newSize
        stateLock.unlock()
        metalLayer?.contentsScale = window?.backingScaleFactor ?? 2
        metalLayer?.drawableSize = CGSize(
            width: newSize.width * (metalLayer?.contentsScale ?? 2),
            height: newSize.height * (metalLayer?.contentsScale ?? 2)
        )
        if renderer == nil { layoutFallbackRing() }
        manageLink()
    }

    public override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        metalLayer?.contentsScale = window?.backingScaleFactor ?? 2
        updateAppearanceFromWindow()
    }

    public override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateAppearanceFromWindow()
    }

    /// GPU time of the last presented frame in ms (-1 when unavailable).
    public var gpuTimeMilliseconds: Double {
        renderer?.lastGPUTimeMilliseconds ?? -1
    }

    /// Frames presented since init (for live fps/CPU readouts).
    public var framesRendered: UInt64 {
        stateLock.lock()
        defer { stateLock.unlock() }
        return _framesRendered
    }

    private var _framesRendered: UInt64 = 0

    /// Headless hook (also handy for CLI 2's screenshot tests): render the
    /// current machine parameters offscreen without touching the layer.
    public func renderTestFrame(
        size: CGSize = CGSize(width: 120, height: 120),
        scale: CGFloat = 2,
        time: TimeInterval = 1.0
    ) -> OrbFrame? {
        guard let renderer else { return nil }
        return try? renderer.render(
            sizePoints: size, scale: scale, params: displayParameters(machine.current),
            time: time, rotation: machine.rotationAngle,
            appearance: effectiveOrbAppearance(), orbitAngle: Float(time * 2)
        )
    }

    // MARK: - Private

    private func effectiveOrbAppearance() -> OrbAppearance {
        if let override = appearanceOverride { return override }
        let name = effectiveAppearance.name
        if name == .aqua || name == .vibrantLight { return .light }
        return .dark
    }

    private func updateAppearanceFromWindow() {
        // Appearance flows into params via halo at tick time; nothing cached.
    }

    private func displayParameters(_ p: OrbParameters) -> OrbParameters {
        var out = p
        if FramePacer.reduceTransparency {
            // Flatter, fully opaque ring when transparency is reduced.
            out.opacity = 1
            out.glow *= 0.5
        }
        return out
    }

    private func levelActive() -> Bool {
        guard let src = levelSource else { return false }
        return max(src.inputLevel, src.outputLevel) > 0.01
    }

    private func manageLink() {
        let settled = OrbParameters.diffMagnitude(machine.current, lastRendered).max() ?? 0 < 0.0005
        let shouldRun = FramePacingPolicy.linkShouldRun(
            state: machine.state, settled: settled,
            levelActive: levelActive(), visible: pacer.isVisible
        )
        pacer.targetFPS = FramePacingPolicy.targetFPS(
            for: machine.state, lowPowerMode: FramePacer.lowPowerMode
        )
        if shouldRun {
            pacer.start()
        } else {
            pacer.stop()
            // One last frame so the cached image matches settled params.
            renderOnce()
        }
    }

    private func tick() {
        let now = Date()
        let dt = min(max(now.timeIntervalSince(lastTickAt), 1e-4), 0.1)
        lastTickAt = now
        let input = levelSource?.inputLevel ?? 0
        let output = levelSource?.outputLevel ?? 0
        let params = machine.update(dt: dt, inputLevel: input, outputLevel: output)
        shaderTime += dt
        let changed = (OrbParameters.diffMagnitude(params, lastRendered).max() ?? 0) > 0.0005
        guard FramePacingPolicy.shouldRender(
            state: machine.state, visible: pacer.isVisible,
            paramsChanged: changed, levelActive: levelActive()
        ) else {
            return
        }
        renderLive(params: params)
        lastRendered = params
        // Re-evaluate: a settled static orb stops the link from here.
        manageLink()
    }

    private func renderOnce() {
        let params = machine.current
        let changed = (OrbParameters.diffMagnitude(params, lastRendered).max() ?? 0) > 0.0005
        guard changed, pacer.isVisible else { return }
        renderLive(params: params)
        lastRendered = params
    }

    private func renderLive(params: OrbParameters) {
        guard let renderer, let layer = metalLayer,
              let drawable = layer.nextDrawable(),
              let buffer = renderer.makeCommandBuffer()
        else { return }
        renderer.encodeFrame(
            to: drawable.texture, commandBuffer: buffer,
            params: displayParameters(params), time: shaderTime,
            rotation: machine.rotationAngle, orbitAngle: Float(shaderTime * 2)
        )
        buffer.present(drawable)
        renderer.observeCompletion(of: buffer)
        buffer.commit()
        stateLock.lock()
        _framesRendered += 1
        stateLock.unlock()
    }

    // MARK: - No-Metal fallback: an honest static ring

    private func installFallbackRing() {
        let ring = CAShapeLayer()
        ring.fillColor = NSColor.clear.cgColor
        ring.strokeColor = NSColor.white.withAlphaComponent(0.5).cgColor
        ring.lineWidth = 3
        layer?.addSublayer(ring)
        fallbackLayer = ring
        layoutFallbackRing()
    }

    private func layoutFallbackRing() {
        guard let ring = fallbackLayer else { return }
        let inset: CGFloat = 6
        let rect = bounds.insetBy(dx: inset, dy: inset)
        ring.path = CGPath(ellipseIn: rect, transform: nil)
        ring.opacity = (machine.state == .offline) ? 0.3 : 0.7
    }
}

/// SwiftUI wrapper for `EvieOrbView`.
public struct OrbView: NSViewRepresentable {
    public var state: OrbState
    public var levelSource: OrbLevelSource?
    public var appearance: OrbAppearance?

    public init(state: OrbState, levelSource: OrbLevelSource? = nil, appearance: OrbAppearance? = nil) {
        self.state = state
        self.levelSource = levelSource
        self.appearance = appearance
    }

    public func makeNSView(context: Context) -> EvieOrbView {
        let view = EvieOrbView(appearance: appearance)
        view.state = state
        view.levelSource = levelSource
        return view
    }

    public func updateNSView(_ view: EvieOrbView, context: Context) {
        view.state = state
        view.levelSource = levelSource
        view.appearanceOverride = appearance
    }
}
