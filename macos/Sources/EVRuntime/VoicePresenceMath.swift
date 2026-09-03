import Foundation

/// Pure placement and audio-level math for the EV.app presence orb.
///
/// Kept in EVRuntime so the menu-bar client and `EVMicTalkTests` share one
/// implementation. The orb itself lives in the EV target.
public enum VoiceOrbSpeechStatus: String, Sendable {
    case hidden
    case preparing
    case speaking
    case ending
}

public enum VoicePresenceMath {
    /// Cropped reference video is 1504×1200. Source was 1600×1200; the crop is
    /// the union of max(R,G,B) ≥ 8 over all 450 frames (x=94…1543, y=0…1199)
    /// plus horizontal margin. Height is uncropped so polar glow is not clipped.
    public static let referenceWidth: Double = 1504
    public static let referenceHeight: Double = 1200
    /// Desktop overlay width in points. Height follows the reference aspect.
    public static let desktopWidth: Double = 280
    public static let tabletWidth: Double = 220
    public static let compactWidth: Double = 168
    public static let panelWidth: Double = desktopWidth
    public static let panelHeight: Double = desktopWidth * referenceHeight / referenceWidth
    /// Sit under the menu-bar clock cluster, not on top of it.
    public static let trailingInset: Double = 8
    public static let menuBarGap: Double = 8
    /// Opacity fade for preparing / ending. Matches the compositor.
    public static let fadeDuration: Double = 0.22
    /// Int16 PCM below this is treated as silence (matches `MicCapture.isQuiet`).
    public static let silenceRMS: Float = 80
    /// Typical close-talk speech lands well below this after smoothing.
    public static let speechRMS: Float = 3_600

    public static func panelSize(visibleWidth: Double) -> (width: Double, height: Double) {
        let width: Double
        if visibleWidth < 700 {
            width = compactWidth
        } else if visibleWidth < 1100 {
            width = tabletWidth
        } else {
            width = desktopWidth
        }
        return (width, width * referenceHeight / referenceWidth)
    }

    /// AppKit origin of a top-right orb just below the menu bar.
    ///
    /// `visibleFrame` already excludes the menu bar (clock / date) and the
    /// Dock, so `maxY` is the first desktop pixel under the clock.
    public static func overlayOrigin(
        visibleX: Double,
        visibleY: Double,
        visibleWidth: Double,
        visibleHeight: Double,
        sizeWidth: Double = panelWidth,
        sizeHeight: Double = panelHeight,
        trailingInset: Double = trailingInset,
        topGap: Double = menuBarGap
    ) -> (x: Double, y: Double) {
        let x = visibleX + visibleWidth - sizeWidth - trailingInset
        let y = visibleY + visibleHeight - sizeHeight - topGap
        return (x, y)
    }

    public static func speechStatus(forAppStatus status: String) -> VoiceOrbSpeechStatus {
        switch status {
        case "speaking":
            return .speaking
        case "thinking":
            return .preparing
        default:
            return .hidden
        }
    }

    public static func speechAudioLevel(status: VoiceOrbSpeechStatus, output: Float) -> Float {
        guard status == .speaking else { return 0 }
        return min(1, max(0, output))
    }

    public static func pcm16RMS(_ data: Data) -> Float {
        let count = data.count / 2
        guard count > 0 else { return 0 }
        var sumSquares: Float = 0
        data.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            let n = min(count, samples.count)
            for index in 0..<n {
                let sample = Float(samples[index].littleEndian)
                sumSquares += sample * sample
            }
        }
        return sqrt(sumSquares / Float(count))
    }

    /// Map raw PCM RMS onto 0...1 with a gentle curve so quiet speech still moves.
    public static func normalizeSpeechRMS(_ rms: Float) -> Float {
        let floor = silenceRMS
        let span = max(speechRMS - floor, 1)
        let linear = max(0, min(1, (rms - floor) / span))
        return pow(linear, 0.55)
    }

    public static func smooth(previous: Float, sample: Float, attack: Float = 0.32, release: Float = 0.14) -> Float {
        let alpha = sample >= previous ? attack : release
        return previous + (sample - previous) * alpha
    }
}
