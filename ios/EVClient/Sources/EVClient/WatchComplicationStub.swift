/// Stub renderer for the Watch complication surface (WORK_BREAKDOWN §13.5).
///
/// A complication is title + up to two compact lines. This keeps the same
/// schema-driven contract as the web/CLI renderers so the real WatchKit
/// target can swap the stub for a timeline provider without changing the
/// HUD payloads.

import Foundation

public enum WatchComplicationStub {
    public struct Layout: Sendable, Equatable {
        public let title: String
        public let lines: [String]

        public init(title: String, lines: [String]) {
            self.title = title
            self.lines = lines
        }
    }

    public static func render(_ card: HUDCard) -> Layout {
        let segments = card.body
            .split(separator: "|", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        return Layout(title: card.title, lines: Array(segments.prefix(2)))
    }

    public static func renderQuickCard(_ card: HUDQuickCard) -> Layout {
        var lines: [String] = []
        if !card.summary.isEmpty {
            lines.append(card.summary)
        }
        if let nextAction = card.nextAction {
            lines.append(nextAction)
        } else if let topRisk = card.topRisk {
            lines.append(topRisk)
        }
        return Layout(title: card.objective, lines: Array(lines.prefix(2)))
    }
}
