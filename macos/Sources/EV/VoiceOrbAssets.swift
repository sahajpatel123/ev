import Foundation

/// Locates the bundled filament-orb media in the packaged app or a SwiftPM run.
enum VoiceOrbAssets {
    static func bundleRoot() -> URL? {
        let fm = FileManager.default
        let bundles = [Bundle.main, Bundle(for: VoiceOrbOverlay.self)]
        var candidates: [URL] = []

        for bundle in bundles {
            if let mp4 = bundle.url(
                forResource: "filament-orb",
                withExtension: "mp4",
                subdirectory: "orb"
            ) {
                candidates.append(mp4.deletingLastPathComponent())
            }
            if let orb = bundle.url(forResource: "orb", withExtension: nil) {
                candidates.append(orb)
            }
            if let resources = bundle.resourceURL {
                candidates.append(resources.appendingPathComponent("orb", isDirectory: true))
                candidates.append(resources.appendingPathComponent("Resources/orb", isDirectory: true))
            }
            candidates.append(
                bundle.bundleURL.appendingPathComponent("Contents/Resources/orb", isDirectory: true)
            )
            candidates.append(
                bundle.bundleURL.appendingPathComponent("Resources/orb", isDirectory: true)
            )
        }

        if let executable = Bundle.main.executableURL {
            let executableDirectory = executable.deletingLastPathComponent()
            candidates.append(
                executableDirectory
                    .appendingPathComponent("../Resources/orb", isDirectory: true)
                    .standardizedFileURL
            )
            candidates.append(
                executableDirectory.appendingPathComponent("Resources/orb", isDirectory: true)
            )
        }

        var visited = Set<String>()
        for candidate in candidates {
            let resolved = candidate.standardizedFileURL.resolvingSymlinksInPath()
            guard visited.insert(resolved.path).inserted else { continue }
            let mp4 = resolved.appendingPathComponent("filament-orb.mp4")
            var isDirectory = ObjCBool(false)
            if fm.fileExists(atPath: mp4.path, isDirectory: &isDirectory),
               !isDirectory.boolValue,
               fm.isReadableFile(atPath: mp4.path) {
                return resolved
            }
        }
        NSLog("EV voice orb: no readable filament-orb.mp4 in the app bundle")
        return nil
    }

    static func videoURL() -> URL? {
        bundleRoot()?.appendingPathComponent("filament-orb.mp4")
    }

    static func stillURL() -> URL? {
        bundleRoot()?.appendingPathComponent("filament-orb-still.png")
    }
}
