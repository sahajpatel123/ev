import Foundation

/// iPhone-only: dynamic Siri/share capture interpreter.
///
/// Turns free-dictated text into a structured action — note, reminder, or
/// timer — by detecting intent words and time expressions anywhere in the
/// phrasing, not just fixed templates. Pure Foundation.
public enum EvieCaptureIntent: String, Equatable, Sendable {
    case note
    case reminder
    case timer
}

public struct EvieCapturePlan: Equatable, Sendable {
    public var intent: EvieCaptureIntent
    public var title: String
    public var delaySeconds: Int?
    public var confidence: Double

    public init(intent: EvieCaptureIntent, title: String, delaySeconds: Int? = nil, confidence: Double) {
        self.intent = intent
        self.title = title
        self.delaySeconds = delaySeconds
        self.confidence = confidence
    }
}

public enum EvieCaptureInterpreter {
    static let reminderWords = ["remind", "reminder", "remember", "don't forget", "dont forget", "todo"]
    static let timerWords = ["timer", "alarm", "countdown", "wake me"]

    public static func interpret(_ text: String) -> EvieCapturePlan {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let lower = trimmed.lowercased()
        guard !trimmed.isEmpty else {
            return .init(intent: .note, title: "", confidence: 0)
        }
        let delay = parseDelay(lower)
        let looksReminder = reminderWords.contains { lower.contains($0) }
        let looksTimer = timerWords.contains { lower.contains($0) }
        if looksTimer || (delay != nil && lower.contains("in ")) {
            let title = stripIntentWords(trimmed, words: timerWords + ["set", "start", "for"])
            return .init(intent: .timer, title: title.isEmpty ? trimmed : title, delaySeconds: delay ?? 60, confidence: looksTimer ? 0.9 : 0.6)
        }
        if looksReminder || delay != nil {
            let title = stripIntentWords(trimmed, words: reminderWords + ["me", "to", "that", "about", "set", "add"])
            return .init(intent: .reminder, title: title.isEmpty ? trimmed : title, delaySeconds: delay, confidence: looksReminder ? 0.85 : 0.55)
        }
        return .init(intent: .note, title: trimmed, confidence: 0.95)
    }

    /// Dynamic duration parse: "10 seconds/minutes/hours", "an hour", "half an hour".
    public static func parseDelay(_ lower: String) -> Int? {
        if lower.contains("half an hour") || lower.contains("half hour") { return 1800 }
        if lower.contains("an hour") || lower.contains("a hour") { return 3600 }
        let pattern = #"(\d+)\s*(second|minute|hour|day)s?"#
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)),
              let numRange = Range(match.range(at: 1), in: lower),
              let unitRange = Range(match.range(at: 2), in: lower),
              let num = Int(lower[numRange]) else { return nil }
        let unit = String(lower[unitRange])
        switch unit {
        case "second": return num
        case "minute": return num * 60
        case "hour": return num * 3600
        case "day": return num * 86400
        default: return nil
        }
    }

    static func stripIntentWords(_ text: String, words: [String]) -> String {
        var out = text
        for word in words {
            out = out.replacingOccurrences(of: word, with: "", options: .caseInsensitive)
        }
        return out.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"'.,!"))
    }
}
