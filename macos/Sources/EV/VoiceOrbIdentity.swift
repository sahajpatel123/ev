import AppKit
import CryptoKit
import Foundation

/// Runtime identity for the orb. Values are logged by the *running* process,
/// not by the compiler, so a stale packaged binary cannot fake them.
enum VoiceOrbIdentity {
    static let buildID = "ORB_BUILD_20260818_0135_visible"
    static let rendererVersion = "metal-video-quad-v4-presence"

    static func report(
        component: String,
        pointer: String,
        extra: [String: String] = [:]
    ) {
        let process = ProcessInfo.processInfo
        let bundle = Bundle.main
        let video = VoiceOrbAssets.videoURL()
        var payload: [String: String] = [
            "component": component,
            "pointer": pointer,
            "buildID": buildID,
            "rendererVersion": rendererVersion,
            "pid": String(process.processIdentifier),
            "executablePath": bundle.executablePath ?? "nil",
            "bundlePath": bundle.bundlePath,
            "resourcePath": bundle.resourcePath ?? "nil",
            "orbSourceFile": video?.path ?? "MISSING",
            "orbSourceSHA256": video.map { sha256Hex(of: $0) } ?? "MISSING",
            "synthetic": ProcessInfo.processInfo.environment["EV_ORB_SYNTHETIC"] ?? "none",
            "debug": ProcessInfo.processInfo.environment["EV_ORB_DEBUG"] ?? "none",
            "force": ProcessInfo.processInfo.environment["EV_ORB_FORCE"] ?? "0",
        ]
        extra.forEach { payload[$0.key] = $0.value }

        let line = payload.keys.sorted().map { "\($0)=\(payload[$0]!)" }.joined(separator: " ")
        NSLog("[ORB-IDENTITY] %@", line)
        NSLog("[ORB] CREATE %@ %@", component, pointer)
        NSLog("ORB BUILD ID: %@", buildID)
        NSLog("ORB RENDERER VERSION: %@", rendererVersion)
        NSLog("EXECUTABLE PATH: %@", payload["executablePath"] ?? "")
        NSLog("BUNDLE PATH: %@", payload["bundlePath"] ?? "")
        NSLog("RESOURCE PATH: %@", payload["resourcePath"] ?? "")
        NSLog("ORB SOURCE FILE: %@", payload["orbSourceFile"] ?? "")
        NSLog("ORB SOURCE SHA256: %@", payload["orbSourceSHA256"] ?? "")
        NSLog("PROCESS PID: %@", payload["pid"] ?? "")

        let url = URL(fileURLWithPath: "/tmp/ev-orb-identity.json")
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url)
        }
    }

    static func logDestroy(component: String, pointer: String) {
        NSLog("[ORB] DESTROY %@ %@", component, pointer)
    }

    static func sha256Hex(of url: URL) -> String {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else {
            return "unreadable"
        }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
