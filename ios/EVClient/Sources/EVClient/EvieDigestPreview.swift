// Cycle EAC-55 — iPhone digest preview line.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieDigestPreview: Equatable, Sendable {
    public var title: String
    public var itemCount: Int

    public init(title: String, itemCount: Int = 0) {
        self.title = title
        self.itemCount = max(0, itemCount)
    }

    public func displayLine() -> String {
        if itemCount == 0 { return title }
        return "\(title) · \(itemCount) items"
    }
}
