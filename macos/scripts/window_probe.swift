import CoreGraphics
import Foundation

/// Prints on-screen CGWindow entries owned by the EV app. Used to verify that
/// `ev://present` overlays are actually visible while EV runs as an accessory.
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
var found = false
for window in list {
    guard let owner = window[kCGWindowOwnerName as String] as? String, owner == "EV" else {
        continue
    }
    found = true
    let name = window[kCGWindowName as String] as? String ?? ""
    let layer = window[kCGWindowLayer as String] as? Int ?? -1
    let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
    print("owner=\(owner) name=\(name) layer=\(layer) bounds=\(bounds)")
}
if !found {
    print("no EV windows on screen")
}
