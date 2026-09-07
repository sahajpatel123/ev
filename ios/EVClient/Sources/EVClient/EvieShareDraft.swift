// Cycle EAC-14 — iPhone share-sheet draft model.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Normalizes what the share extension hands the app (text / URL / file) into
/// one draft the offline queue can capture. Keeps share logic identical on
/// iPhone 16 Pro and iPhone SE. Foundation only; no UIKit so CLT compiles.

import Foundation

public struct EvieShareDraft: Equatable, Sendable {
    public var text: String?
    public var url: String?
    public var filename: String?

    public init(
        text: String? = nil,
        url: String? = nil,
        filename: String? = nil
    ) {
        self.text = text
        self.url = url
        self.filename = filename
    }

    public var isEmpty: Bool {
        let t = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let u = (url ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let f = (filename ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty && u.isEmpty && f.isEmpty
    }

    /// Combined text handed to `CapturePayload(text:)`.
    /// URL and filename are appended as separate lines when present.
    public func captureText() -> String {
        var lines: [String] = []
        let t = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let u = (url ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let f = (filename ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { lines.append(t) }
        if !u.isEmpty { lines.append(u) }
        if !f.isEmpty { lines.append("file: \(f)") }
        return lines.joined(separator: "\n")
    }
}
