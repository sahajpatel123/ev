// Cycle EAC-34 — iPhone call draft validator.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieCallDraft: Equatable, Sendable {
    public enum Kind: String, Sendable { case tel, facetime }
    public var destination: String
    public var kind: Kind

    public init(destination: String = "", kind: Kind = .tel) {
        self.destination = destination
        self.kind = kind
    }

    public var canPlace: Bool {
        !destination.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public func urlScheme() -> String { kind.rawValue }
}
