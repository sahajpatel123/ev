// Cycle EAC-23 — iPhone camera offline queue item.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieCameraQueueItem: Equatable, Sendable {
    public var localPath: String
    public var contentType: String

    public init(localPath: String, contentType: String = "image/jpeg") {
        self.localPath = localPath
        self.contentType = contentType
    }

    public var fileExists: Bool {
        FileManager.default.fileExists(atPath: localPath)
    }

    public func displayName() -> String {
        URL(fileURLWithPath: localPath).lastPathComponent
    }
}
