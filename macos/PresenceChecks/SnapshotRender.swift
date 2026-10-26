import AppKit
import Foundation
import SwiftUI

/// Offscreen snapshot renderer: SwiftUI views → PNG files under
/// `docs/presence-snapshots/`. No windows are ever ordered on screen; the
/// hosting view renders into a cached bitmap only (P5/P6: headless).
@MainActor
enum SnapshotRender {
    /// Repo-rooted snapshot dir, resolved from this file's own path so the
    /// checks work regardless of the invoking working directory.
    static func snapshotsDir() -> URL {
        let here = URL(fileURLWithPath: #filePath)
        // …/macos/PresenceChecks/SnapshotRender.swift → repo root
        let root = here
            .deletingLastPathComponent() // SnapshotRender.swift
            .deletingLastPathComponent() // PresenceChecks
            .deletingLastPathComponent() // macos
        return root.appendingPathComponent("docs/presence-snapshots")
    }

    @discardableResult
    static func render<Content: View>(
        name: String,
        size: NSSize,
        scheme: ColorScheme,
        content: () -> Content
    ) -> URL? {
        let dir = snapshotsDir()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let host = NSHostingView(rootView:
            AnyView(content().environment(\.colorScheme, scheme))
        )
        host.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
        host.frame = NSRect(origin: .zero, size: size)
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            return nil
        }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: NSBitmapImageRep.FileType.png, properties: [:]) else {
            return nil
        }
        let url = dir.appendingPathComponent("\(name).png")
        do {
            try png.write(to: url)
        } catch {
            return nil
        }
        return url
    }

    /// File-level assertions: the pixels are for human review; the checks
    /// prove the render path produced a real image of the right size.
    static func checkPNG(
        _ name: String, scheme: String,
        expectedSize: NSSize,
        check: CheckFn
    ) {
        let url = snapshotsDir().appendingPathComponent("\(name)-\(scheme).png")
        guard let data = try? Data(contentsOf: url) else {
            check("snap-\(name)-\(scheme)", false, "missing \(url.path)")
            return
        }
        guard data.count > 4_000 else {
            check("snap-\(name)-\(scheme)", false, "only \(data.count) bytes")
            return
        }
        guard let image = NSImage(contentsOf: url),
              let rep = image.representations.first as? NSBitmapImageRep
        else {
            check("snap-\(name)-\(scheme)", false, "undecodable png")
            return
        }
        // Offscreen renders inherit the main screen's backing scale (2x on
        // Retina): assert the same integer scale on both axes, not 1x.
        let scaleW = Double(rep.pixelsWide) / expectedSize.width
        let scaleH = Double(rep.pixelsHigh) / expectedSize.height
        let ok = scaleW >= 1 && scaleW == scaleH && scaleW == scaleW.rounded()
        check(
            "snap-\(name)-\(scheme)", ok,
            "\(rep.pixelsWide)x\(rep.pixelsHigh) (scale \(scaleW))"
        )
    }
}
