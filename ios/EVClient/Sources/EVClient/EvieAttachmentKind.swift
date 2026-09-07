// Cycle EAC-36 — iPhone attachment kind detector.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieAttachmentKind: String, Sendable {
    case photo, video, audio, file

    public static func detect(filename: String, contentType: String) -> EvieAttachmentKind {
        let ct = contentType.lowercased()
        if ct.hasPrefix("image/") { return .photo }
        if ct.hasPrefix("video/") { return .video }
        if ct.hasPrefix("audio/") { return .audio }
        let ext = (filename as NSString).pathExtension.lowercased()
        if ["jpg", "jpeg", "png", "heic"].contains(ext) { return .photo }
        if ["mov", "mp4"].contains(ext) { return .video }
        if ["m4a", "wav", "mp3"].contains(ext) { return .audio }
        return .file
    }
}
