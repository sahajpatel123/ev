import AppKit
import AudioToolbox
import AVFoundation
import EVClient
import Foundation

/// Converge: the self-hosted "find my".
///
/// When a converge.beacon intent lands on this Mac (from the
/// iPhone, the SE, or the Mac itself), the Mac answers loudly:
/// a chime, a spoken "here I am", and a screen flash — with the
/// requester's reason on screen. Every execution acks the intent
/// so the receipt log is the convergence proof.
public final class MeshConvergeService: @unchecked Sendable {
    public static let shared = MeshConvergeService()

    private let lock = NSLock()
    private var lastConvergedAt: Date?
    private var lastReason = ""

    private init() {}

    public struct ConvergenceResult: Sendable {
        public let chimed: Bool
        public let spoke: Bool
        public let flashed: Bool
        public let reason: String
    }

    /// Chime + speak + flash. Returns what actually happened.
    @discardableResult
    public func converge(reason: String) -> ConvergenceResult {
        lock.lock()
        lastConvergedAt = Date()
        lastReason = reason
        lock.unlock()

        let chimed = chime()
        let spoke = speak(reason: reason)
        let flashed = flash()
        return ConvergenceResult(chimed: chimed, spoke: spoke, flashed: flashed, reason: reason)
    }

    private func chime() -> Bool {
        // System sound 1005 (Tink) — no audio file needed, works
        // offline, never downloads anything.
        let id: SystemSoundID = 1005
        AudioServicesPlaySystemSound(id)
        return true
    }

    private func speak(reason: String) -> Bool {
        let synthesizer = NSSpeechSynthesizer(voice: nil)
        let phrase = reason.isEmpty ? "Here I am." : "Here I am. \(reason)"
        return synthesizer?.startSpeaking(phrase) == true
    }

    private func flash() -> Bool {
        // Flash the menu-bar icon's window if present; otherwise
        // flash the screen border via a borderless overlay that
        // removes itself after 1.2 s. Never touches the key window.
        DispatchQueue.main.async {
            for window in NSApp.windows where window.isVisible && window.level == .floating {
                window.orderFrontRegardless()
            }
            let overlay = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 0, height: 0),
                styleMask: .borderless,
                backing: .buffered,
                defer: false
            )
            overlay.isReleasedWhenClosed = false
            overlay.backgroundColor = NSColor.systemYellow.withAlphaComponent(0.0)
            overlay.orderFrontRegardless()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                overlay.close()
            }
        }
        return true
    }

    public func lastConvergence() -> (Date?, String) {
        lock.lock()
        defer { lock.unlock() }
        return (lastConvergedAt, lastReason)
    }
}
