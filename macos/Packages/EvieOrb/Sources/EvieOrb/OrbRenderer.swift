import Foundation
import Metal

public enum OrbRendererError: Error, CustomStringConvertible {
    case noDevice
    case shaderNotFound
    case libraryFailed(String)
    case pipelineFailed(String)
    case textureFailed
    case renderFailed(String)

    public var description: String {
        switch self {
        case .noDevice: return "no Metal device"
        case .shaderNotFound: return "orb.metal not found in bundle resources"
        case .libraryFailed(let m): return "shader compile failed: \(m)"
        case .pipelineFailed(let m): return "pipeline failed: \(m)"
        case .textureFailed: return "texture allocation failed"
        case .renderFailed(let m): return "render failed: \(m)"
        }
    }
}

/// Mirror of `OrbUniforms` in orb.metal. Tint first: the 16-byte alignment
/// of the vector head agrees between Swift and Metal; the tail is all floats.
struct OrbUniforms {
    var tint: SIMD4<Float> = SIMD4(1, 1, 1, 0)
    var resolution: SIMD2<Float> = SIMD2(0, 0)
    var time: Float = 0
    var energy: Float = 0
    var radius: Float = 0.62
    var thickness: Float = 0.075
    var turbulence: Float = 0.45
    var flowSpeed: Float = 0.2
    var rotation: Float = 0
    var glow: Float = 0.5
    var stateBlend: Float = 0
    var halo: Float = 0
    var opacity: Float = 0.92
    var orbitStrength: Float = 0
    var orbitAngle: Float = 0
    var bloom: Float = 0
    var coolDim: Float = 0
    var confirmPulse: Float = 0
    var octaveCount: Float = 4
    var reduceMotion: Float = 0
}

/// One offscreen-rendered orb frame: raw BGRA8 premultiplied bytes.
public struct OrbFrame: Sendable {
    public let width: Int
    public let height: Int
    public let bgra: [UInt8]

    public func alphaAt(x: Int, y: Int) -> Float {
        let i = (min(max(y, 0), height - 1) * width + min(max(x, 0), width - 1)) * 4
        return Float(bgra[i + 3]) / 255
    }

    public func maxRGBAt(x: Int, y: Int) -> Float {
        let i = (min(max(y, 0), height - 1) * width + min(max(x, 0), width - 1)) * 4
        return Float(max(bgra[i], max(bgra[i + 1], bgra[i + 2]))) / 255
    }

    public var meanAlpha: Float {
        var sum: UInt64 = 0
        var n: UInt64 = 0
        for i in stride(from: 3, to: bgra.count, by: 4) {
            sum += UInt64(bgra[i])
            n += 1
        }
        guard n > 0 else { return 0 }
        return Float(sum) / Float(n) / 255
    }

    /// Mean per-byte absolute difference (alpha channel, 0...255 scale).
    public func meanAbsDiff(_ other: OrbFrame) -> Float {
        guard width == other.width, height == other.height else { return 255 }
        var sum: UInt64 = 0
        var n: UInt64 = 0
        for i in stride(from: 3, to: bgra.count, by: 4) {
            sum += UInt64(abs(Int(bgra[i]) - Int(other.bgra[i])))
            n += 1
        }
        guard n > 0 else { return 0 }
        return Float(sum) / Float(n)
    }
}

/// Headless Metal orb renderer: parameters in, BGRA texture bytes out.
/// No windows, no layers — the live host (`EvieOrbView`) reuses this same
/// pipeline against a `CAMetalLayer` drawable.
public final class OrbRenderer: @unchecked Sendable {
    private let device: MTLDevice
    private let queue: MTLCommandQueue
    private let pipeline: MTLRenderPipelineState

    /// Noise octaves 1...4 (R5 perf lever; default 4, clamped).
    public var octaveCount: Float = 4

    /// Wall time of the last `render` encode+wait, in milliseconds.
    public private(set) var lastCPUTimeMilliseconds: Double = 0
    /// GPU execution time of the last `render`, in milliseconds, or -1 when
    /// the device does not report command-buffer timestamps.
    public private(set) var lastGPUTimeMilliseconds: Double = -1

    public init() throws {
        guard let device = MTLCreateSystemDefaultDevice() else {
            throw OrbRendererError.noDevice
        }
        self.device = device
        guard let queue = device.makeCommandQueue() else {
            throw OrbRendererError.renderFailed("no command queue")
        }
        self.queue = queue
        let source: String
        if let url = Bundle.module.url(forResource: "orb", withExtension: "metal", subdirectory: "Shaders") {
            source = try String(contentsOf: url, encoding: .utf8)
        } else if let url = Bundle.module.url(forResource: "orb", withExtension: "metal") {
            source = try String(contentsOf: url, encoding: .utf8)
        } else {
            throw OrbRendererError.shaderNotFound
        }
        let library: MTLLibrary
        do {
            library = try device.makeLibrary(source: source, options: nil)
        } catch {
            throw OrbRendererError.libraryFailed(String(describing: error))
        }
        guard let vertex = library.makeFunction(name: "orbVertex"),
              let fragment = library.makeFunction(name: "orbFragment")
        else {
            throw OrbRendererError.libraryFailed("orbVertex/orbFragment not found")
        }
        let desc = MTLRenderPipelineDescriptor()
        desc.vertexFunction = vertex
        desc.fragmentFunction = fragment
        desc.colorAttachments[0].pixelFormat = .bgra8Unorm
        do {
            pipeline = try device.makeRenderPipelineState(descriptor: desc)
        } catch {
            throw OrbRendererError.pipelineFailed(String(describing: error))
        }
    }

    /// Render one frame offscreen at `sizePoints * scale` pixels.
    public func render(
        sizePoints: CGSize,
        scale: CGFloat,
        params: OrbParameters,
        time: TimeInterval,
        rotation: Float,
        appearance: OrbAppearance,
        orbitAngle: Float = 0,
        reduceMotion: Bool = false
    ) throws -> OrbFrame {
        let width = max(1, Int((sizePoints.width * scale).rounded()))
        let height = max(1, Int((sizePoints.height * scale).rounded()))
        let texDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false
        )
        texDesc.usage = [.renderTarget, .shaderRead]
        texDesc.storageMode = .shared
        guard let texture = device.makeTexture(descriptor: texDesc) else {
            throw OrbRendererError.textureFailed
        }
        var uniforms = OrbUniforms()
        uniforms.tint = SIMD4(params.tintR, params.tintG, params.tintB, 0)
        uniforms.resolution = SIMD2(Float(width), Float(height))
        uniforms.time = Float(time)
        uniforms.energy = params.energy
        uniforms.radius = params.radius
        uniforms.thickness = params.thickness
        uniforms.turbulence = params.turbulence
        uniforms.flowSpeed = params.flowSpeed
        uniforms.rotation = rotation
        uniforms.glow = params.glow
        uniforms.stateBlend = params.confirmPulse
        uniforms.halo = params.halo
        uniforms.opacity = params.opacity
        uniforms.orbitStrength = params.orbitStrength
        uniforms.orbitAngle = orbitAngle
        uniforms.bloom = params.bloom
        uniforms.coolDim = params.coolDim
        uniforms.confirmPulse = params.confirmPulse
        uniforms.octaveCount = min(4, max(1, octaveCount))
        uniforms.reduceMotion = reduceMotion ? 1 : 0

        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 0)
        pass.colorAttachments[0].storeAction = .store

        guard let buffer = queue.makeCommandBuffer(),
              let encoder = buffer.makeRenderCommandEncoder(descriptor: pass)
        else {
            throw OrbRendererError.renderFailed("no command buffer/encoder")
        }
        let start = Date()
        encoder.setRenderPipelineState(pipeline)
        withUnsafeBytes(of: uniforms) { bytes in
            encoder.setFragmentBytes(bytes.baseAddress!, length: bytes.count, index: 0)
        }
        encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        encoder.endEncoding()
        buffer.commit()
        buffer.waitUntilCompleted()
        lastCPUTimeMilliseconds = Date().timeIntervalSince(start) * 1000
        if let err = buffer.error {
            throw OrbRendererError.renderFailed(String(describing: err))
        }
        let gpuStart = buffer.gpuStartTime
        let gpuEnd = buffer.gpuEndTime
        lastGPUTimeMilliseconds = (gpuEnd > gpuStart) ? (gpuEnd - gpuStart) * 1000 : -1

        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        texture.getBytes(
            &bytes, bytesPerRow: width * 4,
            from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0
        )
        _ = appearance // appearance is carried in params.halo; kept for API clarity
        return OrbFrame(width: width, height: height, bgra: bytes)
    }
}
