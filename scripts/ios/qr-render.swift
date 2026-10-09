// Render one QR PNG with zero dependencies (CoreImage, macOS only).
// Usage: swift qr-render.swift <text> <out.png> [min-pixels]
// Exit 0 on success; diagnostics go to stderr, never the PNG path.
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation

#if !canImport(AppKit)
fputs("qr-render.swift needs macOS (CoreImage/AppKit).\n", stderr)
exit(2)
#else
import AppKit

guard CommandLine.arguments.count >= 3 else {
    fputs("usage: qr-render.swift <text> <out.png> [min-pixels]\n", stderr)
    exit(2)
}
let text = CommandLine.arguments[1]
let outPath = CommandLine.arguments[2]
let minPixels = Double(CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : "480") ?? 480
guard !text.isEmpty else {
    fputs("qr-render.swift: refusing to encode empty text.\n", stderr)
    exit(2)
}

let filter = CIFilter.qrCodeGenerator()
filter.message = Data(text.utf8)
filter.correctionLevel = "M"
guard let code = filter.outputImage else {
    fputs("qr-render.swift: QR encode failed.\n", stderr)
    exit(1)
}
// Integer scale-up keeps edges crisp for phone cameras.
let modules = max(code.extent.width, code.extent.height)
let scale = max(1.0, ceil(minPixels / modules))
let scaled = code.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
let context = CIContext()
guard let cg = context.createCGImage(scaled, from: scaled.extent) else {
    fputs("qr-render.swift: rasterize failed.\n", stderr)
    exit(1)
}
let rep = NSBitmapImageRep(cgImage: cg)
rep.size = NSSize(width: scaled.extent.width, height: scaled.extent.height)
guard let png = rep.representation(using: .png, properties: [:]) else {
    fputs("qr-render.swift: PNG encode failed.\n", stderr)
    exit(1)
}
do {
    try png.write(to: URL(fileURLWithPath: outPath))
} catch {
    fputs("qr-render.swift: write failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
#endif
