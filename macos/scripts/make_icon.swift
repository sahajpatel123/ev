#!/usr/bin/env swift
// Generates Resources/EV.icns from scratch (no asset catalog needed).
// Draws a macOS-style rounded-rect tile with an "EV" monogram and a pulse.
//
// Usage: swift macos/scripts/make_icon.swift
import AppKit

let size: CGFloat = 1024
let image = NSImage(size: NSSize(width: size, height: size))
image.lockFocus()

guard let ctx = NSGraphicsContext.current?.cgContext else {
    fatalError("no graphics context")
}

// --- Background: rounded tile with vertical indigo -> violet gradient ---
let tileRect = NSRect(x: 0, y: 0, width: size, height: size)
let tilePath = NSBezierPath(
    roundedRect: tileRect,
    xRadius: size * 0.22,
    yRadius: size * 0.22
)
tilePath.addClip()

let colors = [
    NSColor(calibratedRed: 0.09, green: 0.07, blue: 0.24, alpha: 1).cgColor,   // deep indigo
    NSColor(calibratedRed: 0.24, green: 0.16, blue: 0.55, alpha: 1).cgColor,   // violet
    NSColor(calibratedRed: 0.44, green: 0.25, blue: 0.72, alpha: 1).cgColor,   // purple
] as CFArray
let gradient = CGGradient(
    colorsSpace: CGColorSpaceCreateDeviceRGB(),
    colors: colors,
    locations: [0, 0.55, 1]
)!
ctx.drawLinearGradient(
    gradient,
    start: CGPoint(x: size * 0.5, y: size),
    end: CGPoint(x: size * 0.5, y: 0),
    options: []
)

// --- Soft radial glow behind the monogram ---
let glowColors = [
    NSColor(calibratedWhite: 1, alpha: 0.22).cgColor,
    NSColor(calibratedWhite: 1, alpha: 0.0).cgColor,
] as CFArray
let glow = CGGradient(
    colorsSpace: CGColorSpaceCreateDeviceRGB(),
    colors: glowColors,
    locations: [0, 1]
)!
let glowCenter = CGPoint(x: size * 0.5, y: size * 0.54)
ctx.drawRadialGradient(
    glow,
    startCenter: glowCenter,
    startRadius: 0,
    endCenter: glowCenter,
    endRadius: size * 0.5,
    options: []
)

// --- "EV" monogram ---
let monogram = "EV" as NSString
let font = NSFont(name: "SFProDisplay-Bold", size: 430)
    ?? NSFont.systemFont(ofSize: 430, weight: .heavy)
let attrs: [NSAttributedString.Key: Any] = [
    .font: font,
    .foregroundColor: NSColor.white,
]
let textSize = monogram.size(withAttributes: attrs)
let textRect = NSRect(
    x: (size - textSize.width) / 2,
    y: (size - textSize.height) / 2 + size * 0.06,
    width: textSize.width,
    height: textSize.height
)
monogram.draw(in: textRect, withAttributes: attrs)

// --- Pulse line under the monogram ---
let pulsePath = NSBezierPath()
let pulseY = textRect.minY - size * 0.09
let half = size * 0.26
pulsePath.move(to: NSPoint(x: size * 0.5 - half, y: pulseY))
pulsePath.line(to: NSPoint(x: size * 0.5 - half * 0.45, y: pulseY))
pulsePath.line(to: NSPoint(x: size * 0.5, y: pulseY + size * 0.045))
pulsePath.line(to: NSPoint(x: size * 0.5 + half * 0.45, y: pulseY))
pulsePath.line(to: NSPoint(x: size * 0.5 + half, y: pulseY))
pulsePath.lineWidth = 26
pulsePath.lineCapStyle = .round
pulsePath.lineJoinStyle = .round
NSColor(calibratedRed: 0.35, green: 0.85, blue: 1.0, alpha: 1).setStroke()
pulsePath.stroke()

image.unlockFocus()

// --- Write the icon set ---
let base = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    .appendingPathComponent("build/EV.iconset")
try? FileManager.default.removeItem(at: base)
try! FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)

let specs: [(String, Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

guard let tiff = image.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff) else {
    fatalError("failed to render source image")
}

for (name, px) in specs {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: px,
        pixelsHigh: px,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else { fatalError("failed to create bitmap for \(name)") }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    rep.draw(in: NSRect(x: 0, y: 0, width: px, height: px))
    NSGraphicsContext.restoreGraphicsState()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        fatalError("failed to encode \(name)")
    }
    try! png.write(to: base.appendingPathComponent(name))
}

print("Wrote \(base.path)")

// --- Assemble the icns ---
let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
let icnsURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    .appendingPathComponent("Resources/EV.icns")
process.arguments = ["-c", "icns", base.path, "-o", icnsURL.path]
try! process.run()
process.waitUntilExit()
print("Wrote \(icnsURL.path) (exit \(process.terminationStatus))")
