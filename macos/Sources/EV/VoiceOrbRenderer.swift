import AppKit
import AVFoundation
import CoreVideo
import EVRuntime
import ImageIO
import Metal
import QuartzCore

/// Video quad compositor. There is no sphere, edge glow, bloom pass, or
/// circular mask. The MP4 is emissive light authored over black; that black
/// also hides a low-frequency spherical bloom. `alpha = max(R,G,B)` with
/// source-over turns that bloom into a dark globe and a blue circumference.
/// Keep only local ridges (filaments / yellow accents) and write zeros
/// everywhere else so the window server has nothing to composite.
private let orbShaderSource = """
#include <metal_stdlib>
using namespace metal;

struct VertexOut {
    float4 position [[position]];
    float2 uv;
};

struct Uniforms {
    float opacity;
    float debugMode;
};

constant float DOG = 3.0 / 255.0;
constant float CONTRAST = 8.0 / 255.0;
constant float SAT = 5.0 / 255.0;
constant float BLUE = 4.0 / 255.0;
constant float BRIGHT = 20.0 / 255.0;
constant float YELLOW_RG = 32.0 / 255.0;
constant float YELLOW_BIAS = 12.0 / 255.0;

vertex VertexOut orbVertex(uint vid [[vertex_id]]) {
    float2 positions[4] = {
        float2(-1.0, -1.0),
        float2( 1.0, -1.0),
        float2(-1.0,  1.0),
        float2( 1.0,  1.0)
    };
    float2 uvs[4] = {
        float2(0.0, 1.0),
        float2(1.0, 1.0),
        float2(0.0, 0.0),
        float2(1.0, 0.0)
    };
    VertexOut out;
    out.position = float4(positions[vid], 0.0, 1.0);
    out.uv = uvs[vid];
    return out;
}

fragment float4 orbFragment(VertexOut in [[stage_in]],
                            constant Uniforms &u [[buffer(0)]],
                            texture2d<float> tex [[texture(0)]],
                            sampler samp [[sampler(0)]]) {
    // Synthetic identity probes. These ignore the video on purpose.
    // 3 = fully transparent, 4 = solid magenta quad, 5 = diagonal line.
    if (u.debugMode > 4.5) {
        float d = abs(in.uv.x - in.uv.y);
        float line = 1.0 - smoothstep(0.035, 0.055, d);
        return float4(1.0, 0.0, 0.55, 1.0) * line * u.opacity;
    }
    if (u.debugMode > 3.5) {
        return float4(1.0, 0.0, 1.0, 1.0) * u.opacity;
    }
    if (u.debugMode > 2.5) {
        return float4(0.0, 0.0, 0.0, 0.0);
    }

    float3 rgb = tex.sample(samp, in.uv).rgb;
    float energy = max(rgb.r, max(rgb.g, rgb.b));
    float2 texel = 1.0 / float2(tex.get_width(), tex.get_height());
    float acc = 0.0;
    float mx = energy;
    float mn = energy;
    for (int y = -2; y <= 2; ++y) {
        for (int x = -2; x <= 2; ++x) {
            float3 nrgb = tex.sample(samp, in.uv + float2(x, y) * texel).rgb;
            float e = max(nrgb.r, max(nrgb.g, nrgb.b));
            acc += e;
            if (abs(x) <= 1 && abs(y) <= 1) {
                mx = max(mx, e);
                mn = min(mn, e);
            }
        }
    }
    float dog = energy - acc / 25.0;
    float contrast = mx - mn;
    float sat = energy - min(rgb.r, min(rgb.g, rgb.b));
    float blueBias = max(rgb.b - max(rgb.r, rgb.g), 0.0);
    bool yellow = rgb.r >= YELLOW_RG && rgb.g >= YELLOW_RG
        && (rgb.r + rgb.g) > (rgb.b + YELLOW_BIAS);
    bool ridge = dog >= DOG && contrast >= CONTRAST
        && (sat >= SAT || blueBias >= BLUE || energy >= BRIGHT);
    bool keep = yellow || ridge;
    float alpha = keep ? energy : 0.0;
    float3 outRGB = float3(0.0);
    if (keep) {
        float luma = dot(rgb, float3(0.22, 0.40, 0.38));
        float3 sat = mix(float3(luma), rgb, 1.38);
        float3 graded = (sat - 0.18) * 1.24 + 0.18;
        outRGB = clamp(graded, 0.0, 1.0);
    }

    if (u.debugMode > 1.5) {
        return float4(alpha, alpha, alpha, 1.0) * u.opacity;
    }
    if (u.debugMode > 0.5) {
        return float4(rgb * u.opacity, u.opacity);
    }
    return float4(outRGB * u.opacity, alpha * u.opacity);
}
"""

private struct OrbUniforms {
    var opacity: Float
    var debugMode: Float
}

private struct OrbRenderTarget {
    let pixelBuffer: CVPixelBuffer
    let cvTexture: CVMetalTexture
    let texture: MTLTexture
    var inFlight = false
}

enum VoiceOrbDebug {
    /// 0 ridge, 1 raw source, 2 alpha vis, 3 transparent, 4 magenta, 5 diagonal.
    static var mode: Float {
        switch ProcessInfo.processInfo.environment["EV_ORB_SYNTHETIC"]?.lowercased() {
        case "transparent": return 3
        case "magenta": return 4
        case "diagonal": return 5
        case "source": return 1
        case "ridge": return 0
        default: break
        }
        switch ProcessInfo.processInfo.environment["EV_ORB_DEBUG"]?.lowercased() {
        case "source": return 1
        case "alpha": return 2
        default: return 0
        }
    }

    static var showsChecker: Bool {
        ProcessInfo.processInfo.environment["EV_ORB_DEBUG"]?.lowercased() == "checker"
    }

    static var showsMarker: Bool {
        let env = ProcessInfo.processInfo.environment
        if env["EV_ORB_MARKER"] == "1" { return true }
        if env["EV_ORB_SYNTHETIC"] != nil { return true }
        if let debug = env["EV_ORB_DEBUG"], !debug.isEmpty { return true }
        return false
    }

    static var forceVisible: Bool {
        ProcessInfo.processInfo.environment["EV_ORB_FORCE"] == "1"
            || ProcessInfo.processInfo.environment["EV_ORB_SYNTHETIC"] != nil
    }

    static var isSyntheticShape: Bool {
        let mode = Self.mode
        return mode > 2.5
    }
}

final class VoiceOrbLayerView: NSView {
    private var frameImage: CGImage?

    override var isOpaque: Bool { false }
    override var wantsDefaultClipping: Bool { false }
    override var allowsVibrancy: Bool { false }
    override var wantsUpdateLayer: Bool { false }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = false
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = false
    }

    func setFrameImage(_ image: CGImage?) {
        frameImage = image
        needsDisplay = true
    }

    func applyTransparentLayer() {
        wantsLayer = false
        window?.isOpaque = false
        window?.backgroundColor = .clear
        window?.hasShadow = false
        needsDisplay = true
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyTransparentLayer()
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.setBlendMode(.copy)
        ctx.clear(bounds)
        guard let frameImage else { return }
        ctx.saveGState()
        ctx.translateBy(x: 0, y: bounds.height)
        ctx.scaleBy(x: 1, y: -1)
        ctx.setBlendMode(.normal)
        ctx.interpolationQuality = .low
        ctx.draw(frameImage, in: bounds)
        ctx.restoreGState()
    }
}

@MainActor
final class VoiceOrbRenderer: NSObject {
    let view: VoiceOrbLayerView

    private let device: MTLDevice
    private let commandQueue: MTLCommandQueue
    private let pipeline: MTLRenderPipelineState
    private let sampler: MTLSamplerState
    private var textureCache: CVMetalTextureCache?
    private var targets: [OrbRenderTarget] = []
    private var writeIndex = 0
    private var stillTexture: MTLTexture?
    private var stillImage: CGImage?
    private var videoTexture: MTLTexture?
    private var videoCVTexture: CVMetalTexture?
    private var videoSize = SIMD2<Int>(0, 0)
    private var dumpName: String?

    private var player: AVPlayer?
    private var playerItem: AVPlayerItem?
    private var videoOutput: AVPlayerItemVideoOutput?
    private var loopObserver: NSObjectProtocol?
    private var frameTimer: Timer?

    private var status: VoiceOrbSpeechStatus = .hidden
    private var reduceMotion = false
    private var opacity: Float = 0
    private var opacityTarget: Float = 0
    private var lastDraw: CFTimeInterval = 0
    private var wantsPlayback = false
    private var presentedFrames = 0
    private var lastFrameChecksum: UInt64 = 0

    init?(frame: NSRect) {
        guard let device = MTLCreateSystemDefaultDevice(),
              let commandQueue = device.makeCommandQueue() else {
            NSLog("EV voice orb: Metal is unavailable")
            return nil
        }
        self.device = device
        self.commandQueue = commandQueue

        let view = VoiceOrbLayerView(frame: frame)
        view.autoresizingMask = [.width, .height]
        self.view = view

        let library: MTLLibrary
        do {
            library = try device.makeLibrary(source: orbShaderSource, options: nil)
        } catch {
            NSLog("EV voice orb: Metal shader compile failed: %@", error.localizedDescription)
            return nil
        }
        guard let vertex = library.makeFunction(name: "orbVertex"),
              let fragment = library.makeFunction(name: "orbFragment") else {
            return nil
        }
        let desc = MTLRenderPipelineDescriptor()
        desc.vertexFunction = vertex
        desc.fragmentFunction = fragment
        desc.colorAttachments[0].pixelFormat = .bgra8Unorm
        desc.colorAttachments[0].isBlendingEnabled = false
        do {
            pipeline = try device.makeRenderPipelineState(descriptor: desc)
        } catch {
            NSLog("EV voice orb: Metal pipeline failed: %@", error.localizedDescription)
            return nil
        }

        let sample = MTLSamplerDescriptor()
        sample.minFilter = .linear
        sample.magFilter = .linear
        sample.sAddressMode = .clampToEdge
        sample.tAddressMode = .clampToEdge
        guard let sampler = device.makeSamplerState(descriptor: sample) else { return nil }
        self.sampler = sampler

        var cache: CVMetalTextureCache?
        guard CVMetalTextureCacheCreate(kCFAllocatorDefault, nil, device, nil, &cache) == kCVReturnSuccess,
              let cache else {
            return nil
        }
        self.textureCache = cache

        super.init()
        VoiceOrbIdentity.report(
            component: "VoiceOrbRenderer",
            pointer: String(describing: ObjectIdentifier(self)),
            extra: [
                "shaderMode": String(VoiceOrbDebug.mode),
                "metalDevice": device.name,
            ]
        )
        if VoiceOrbDebug.isSyntheticShape {
            makeDummyTexture()
        }
        loadStill()
        loadVideo()
        if VoiceOrbDebug.isSyntheticShape {
            opacity = 1
            opacityTarget = 1
            startTimer()
        } else if let stillImage {
            view.setFrameImage(stillImage)
        }
    }

    func setState(status: VoiceOrbSpeechStatus, audioLevel: Float, reduceMotion: Bool) {
        _ = audioLevel
        self.status = status
        self.reduceMotion = reduceMotion
        let visible = status == .preparing || status == .speaking || VoiceOrbDebug.forceVisible
        if VoiceOrbDebug.isSyntheticShape || VoiceOrbDebug.forceVisible {
            opacity = 1
            opacityTarget = 1
        } else {
            opacityTarget = visible ? 1 : 0
        }
        wantsPlayback = visible && !reduceMotion && !VoiceOrbDebug.isSyntheticShape
        if visible {
            startTimer()
            if wantsPlayback {
                startPlayback()
            } else {
                player?.pause()
            }
        } else if opacity <= 0.001 {
            pausePlayback()
            stopTimer()
            view.setFrameImage(nil)
        } else {
            startTimer()
        }
        view.applyTransparentLayer()
    }

    func pause() {
        wantsPlayback = false
        pausePlayback()
        stopTimer()
        view.setFrameImage(nil)
    }

    func resume() {
        let visible = status == .preparing || status == .speaking || VoiceOrbDebug.forceVisible
        if visible {
            startTimer()
            if !reduceMotion {
                wantsPlayback = true
                startPlayback()
            }
        }
    }

    func seek(seconds: Double) {
        let time = CMTime(seconds: seconds, preferredTimescale: 600)
        player?.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
        dumpName = String(format: "t%.1f", seconds)
        pullVideoFrame()
        tick()
    }

    private func startTimer() {
        guard frameTimer == nil else { return }
        lastDraw = 0
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.tick()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        frameTimer = timer
        tick()
    }

    private func stopTimer() {
        frameTimer?.invalidate()
        frameTimer = nil
    }

    private func tick() {
        let now = CACurrentMediaTime()
        if lastDraw == 0 { lastDraw = now }
        let dt = Float(min(0.05, max(0, now - lastDraw)))
        lastDraw = now
        let fade = Float(VoicePresenceMath.fadeDuration)
        if fade > 0 {
            if opacity < opacityTarget {
                opacity = min(opacityTarget, opacity + dt / fade)
            } else if opacity > opacityTarget {
                opacity = max(opacityTarget, opacity - dt / fade)
            }
        } else {
            opacity = opacityTarget
        }

        if opacity <= 0.001, opacityTarget <= 0 {
            pausePlayback()
            stopTimer()
            view.setFrameImage(nil)
            return
        }

        if wantsPlayback {
            pullVideoFrame()
        }

        let source: MTLTexture? = (wantsPlayback ? videoTexture : nil) ?? stillTexture
        guard let source else {
            if VoiceOrbDebug.isSyntheticShape {
                NSLog("[ORB] tick has no dummy texture; synthetic probe cannot run")
            } else if let stillImage {
                view.setFrameImage(stillImage)
            }
            return
        }

        ensureTargets(width: source.width, height: source.height)
        guard !targets.isEmpty else { return }
        if targets[writeIndex].inFlight {
            writeIndex = 1 - writeIndex
        }
        guard !targets[writeIndex].inFlight else { return }
        render(source: source, targetIndex: writeIndex)
        writeIndex = 1 - writeIndex
    }

    private func render(source: MTLTexture, targetIndex: Int) {
        guard targetIndex < targets.count else { return }
        var target = targets[targetIndex]
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = target.texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].storeAction = .store
        pass.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 0)
        guard let commandBuffer = commandQueue.makeCommandBuffer(),
              let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: pass) else {
            return
        }
        encoder.setRenderPipelineState(pipeline)
        var uniforms = OrbUniforms(opacity: opacity, debugMode: VoiceOrbDebug.mode)
        encoder.setFragmentBytes(&uniforms, length: MemoryLayout<OrbUniforms>.stride, index: 0)
        encoder.setFragmentSamplerState(sampler, index: 0)
        encoder.setFragmentTexture(source, index: 0)
        encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        encoder.endEncoding()

        target.inFlight = true
        targets[targetIndex] = target
        commandBuffer.addCompletedHandler { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, targetIndex < self.targets.count else { return }
                self.targets[targetIndex].inFlight = false
                guard self.opacity > 0.001 else { return }
                if let image = Self.premultipliedImage(from: self.targets[targetIndex].pixelBuffer) {
                    self.view.setFrameImage(image)
                    self.notePresentedFrame(self.targets[targetIndex].pixelBuffer)
                    self.dumpIfNeeded(image)
                }
            }
        }
        commandBuffer.commit()
    }

    private func dumpIfNeeded(_ image: CGImage) {
        guard ProcessInfo.processInfo.environment["EV_ORB_DUMP"] != nil,
              let name = dumpName else { return }
        dumpName = nil
        let url = URL(fileURLWithPath: "/tmp/ev-orb-metal-\(name).png")
        guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
            return
        }
        CGImageDestinationAddImage(dest, image, nil)
        CGImageDestinationFinalize(dest)
        NSLog("EV voice orb: dumped %@", url.path)
    }

    private func ensureTargets(width: Int, height: Int) {
        if videoSize.x == width, videoSize.y == height, targets.count == 2 { return }
        targets.removeAll()
        videoSize = SIMD2(width, height)
        for _ in 0..<2 {
            if let target = makeTarget(width: width, height: height) {
                targets.append(target)
            }
        }
    }

    private func makeTarget(width: Int, height: Int) -> OrbRenderTarget? {
        guard let cache = textureCache else { return nil }
        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
            kCVPixelBufferMetalCompatibilityKey as String: true,
            kCVPixelBufferIOSurfacePropertiesKey as String: [:] as [String: Any],
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
        ]
        var pixelBuffer: CVPixelBuffer?
        let status = CVPixelBufferCreate(
            kCFAllocatorDefault,
            width,
            height,
            kCVPixelFormatType_32BGRA,
            attrs as CFDictionary,
            &pixelBuffer
        )
        guard status == kCVReturnSuccess, let pixelBuffer else { return nil }
        var cvTexture: CVMetalTexture?
        let texStatus = CVMetalTextureCacheCreateTextureFromImage(
            kCFAllocatorDefault,
            cache,
            pixelBuffer,
            nil,
            .bgra8Unorm,
            width,
            height,
            0,
            &cvTexture
        )
        guard texStatus == kCVReturnSuccess,
              let cvTexture,
              let texture = CVMetalTextureGetTexture(cvTexture) else {
            return nil
        }
        return OrbRenderTarget(pixelBuffer: pixelBuffer, cvTexture: cvTexture, texture: texture)
    }

    static func premultipliedImage(from pixelBuffer: CVPixelBuffer) -> CGImage? {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixelBuffer) else { return nil }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let data = Data(bytes: base, count: bytesPerRow * height)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let bitmapInfo = CGBitmapInfo.byteOrder32Little.union(
            CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue)
        )
        guard let provider = CGDataProvider(data: data as CFData) else { return nil }
        return CGImage(
            width: width,
            height: height,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: bitmapInfo,
            provider: provider,
            decode: nil,
            shouldInterpolate: true,
            intent: .defaultIntent
        )
    }

    private func makeDummyTexture() {
        let width = 16
        let height = 16
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm,
            width: width,
            height: height,
            mipmapped: false
        )
        descriptor.usage = [.shaderRead]
        guard let texture = device.makeTexture(descriptor: descriptor) else { return }
        let bytes = [UInt8](repeating: 0, count: width * height * 4)
        bytes.withUnsafeBytes { raw in
            texture.replace(
                region: MTLRegionMake2D(0, 0, width, height),
                mipmapLevel: 0,
                withBytes: raw.baseAddress!,
                bytesPerRow: width * 4
            )
        }
        stillTexture = texture
        NSLog("[ORB] dummy texture installed for synthetic mode %.1f", VoiceOrbDebug.mode)
    }

    private func notePresentedFrame(_ pixelBuffer: CVPixelBuffer) {
        presentedFrames += 1
        let checksum = Self.sampleChecksum(pixelBuffer)
        if presentedFrames <= 3 || presentedFrames % 30 == 0 || checksum != lastFrameChecksum {
            let seconds = player?.currentTime().seconds ?? -1
            NSLog(
                "[ORB] FRAME n=%d t=%.3f checksum=%llu playerPlaying=%d",
                presentedFrames,
                seconds,
                checksum,
                (player?.timeControlStatus == .playing) ? 1 : 0
            )
        }
        lastFrameChecksum = checksum
    }

    private static func sampleChecksum(_ pixelBuffer: CVPixelBuffer) -> UInt64 {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixelBuffer) else { return 0 }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let stride = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let ptr = base.assumingMemoryBound(to: UInt8.self)
        var hash: UInt64 = 14695981039346656037
        var y = 0
        while y < height {
            var x = 0
            while x < width {
                let off = y * stride + x * 4
                hash ^= UInt64(ptr[off]) &+ UInt64(ptr[off + 1]) << 8 &+ UInt64(ptr[off + 2]) << 16 &+ UInt64(ptr[off + 3]) << 24
                hash &*= 109951223
                x += 48
            }
            y += 48
        }
        return hash
    }

    private func loadStill() {
        guard let url = VoiceOrbAssets.stillURL() else { return }
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            return
        }
        stillImage = image
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm,
            width: image.width,
            height: image.height,
            mipmapped: false
        )
        descriptor.usage = [.shaderRead]
        guard let texture = device.makeTexture(descriptor: descriptor) else { return }
        let cs = CGColorSpaceCreateDeviceRGB()
        let info = CGBitmapInfo.byteOrder32Little.union(
            CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue)
        )
        var bytes = [UInt8](repeating: 0, count: image.width * image.height * 4)
        bytes.withUnsafeMutableBytes { raw in
            if let ctx = CGContext(
                data: raw.baseAddress,
                width: image.width,
                height: image.height,
                bitsPerComponent: 8,
                bytesPerRow: image.width * 4,
                space: cs,
                bitmapInfo: info.rawValue
            ) {
                ctx.clear(CGRect(x: 0, y: 0, width: image.width, height: image.height))
                ctx.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
            }
        }
        bytes.withUnsafeBytes { raw in
            texture.replace(
                region: MTLRegionMake2D(0, 0, image.width, image.height),
                mipmapLevel: 0,
                withBytes: raw.baseAddress!,
                bytesPerRow: image.width * 4
            )
        }
        stillTexture = texture
    }

    private func loadVideo() {
        guard let url = VoiceOrbAssets.videoURL() else {
            NSLog("EV voice orb: filament-orb.mp4 missing")
            return
        }
        let item = AVPlayerItem(url: url)
        let output = AVPlayerItemVideoOutput(pixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
            kCVPixelBufferMetalCompatibilityKey as String: true,
        ])
        item.add(output)
        let player = AVPlayer(playerItem: item)
        player.isMuted = true
        player.automaticallyWaitsToMinimizeStalling = false
        if #available(macOS 12.0, *) {
            player.preventsDisplaySleepDuringVideoPlayback = false
        }
        player.actionAtItemEnd = .none
        loopObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: item,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.replayIfNeeded()
            }
        }
        self.playerItem = item
        self.videoOutput = output
        self.player = player
    }

    private func replayIfNeeded() {
        guard wantsPlayback else { return }
        player?.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            guard finished else { return }
            Task { @MainActor in
                if self?.wantsPlayback == true {
                    self?.player?.play()
                }
            }
        }
    }

    private func startPlayback() {
        guard let player else { return }
        if player.timeControlStatus != .playing {
            player.play()
        }
    }

    private func pausePlayback() {
        player?.pause()
    }

    private func pullVideoFrame() {
        guard let output = videoOutput, let cache = textureCache else { return }
        let time = output.itemTime(forHostTime: CACurrentMediaTime())
        guard output.hasNewPixelBuffer(forItemTime: time) else { return }
        var displayTime = CMTime.zero
        guard let pixelBuffer = output.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: &displayTime) else {
            return
        }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        var cvTexture: CVMetalTexture?
        let status = CVMetalTextureCacheCreateTextureFromImage(
            kCFAllocatorDefault,
            cache,
            pixelBuffer,
            nil,
            .bgra8Unorm,
            width,
            height,
            0,
            &cvTexture
        )
        guard status == kCVReturnSuccess, let cvTexture, let texture = CVMetalTextureGetTexture(cvTexture) else {
            return
        }
        videoCVTexture = cvTexture
        videoTexture = texture
    }
}

final class VoiceOrbHostView: NSView {
    override var isOpaque: Bool { VoiceOrbDebug.showsChecker }
    override var wantsDefaultClipping: Bool { false }
    override var allowsVibrancy: Bool { false }
    override var wantsUpdateLayer: Bool { false }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func draw(_ dirtyRect: NSRect) {
        if VoiceOrbDebug.showsChecker {
            let cell: CGFloat = 16
            var row = 0
            var y: CGFloat = 0
            while y < bounds.height {
                var col = 0
                var x: CGFloat = 0
                while x < bounds.width {
                    ((row + col) % 2 == 0 ? NSColor(calibratedWhite: 0.94, alpha: 1) : NSColor(calibratedWhite: 0.2, alpha: 1)).setFill()
                    NSRect(x: x, y: y, width: cell, height: cell).fill()
                    x += cell
                    col += 1
                }
                y += cell
                row += 1
            }
            return
        }
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.setBlendMode(.copy)
        ctx.clear(bounds)
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = false
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = false
    }
}

final class VoiceOrbStillView: NSView {
    override var isOpaque: Bool { false }
    override var allowsVibrancy: Bool { false }
    override var wantsUpdateLayer: Bool { false }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = false
        NSLog("[ORB] CREATE VoiceOrbStillView %@", String(describing: ObjectIdentifier(self)))
    }

    convenience init(image: NSImage?, frame frameRect: NSRect) {
        self.init(frame: frameRect)
        _ = image
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = false
    }

    override func draw(_ dirtyRect: NSRect) {
        // Identity probe: a fallback must never look like the globe.
        NSColor.systemRed.setFill()
        bounds.fill()
        let text = "ORB FALLBACK\n\(VoiceOrbIdentity.buildID)"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .bold),
            .foregroundColor: NSColor.white,
        ]
        (text as NSString).draw(in: bounds.insetBy(dx: 8, dy: 8), withAttributes: attrs)
    }
}
