import AppKit

/// Policy for turning the menu-bar panel into a real text-entry surface.
///
/// EV ships as an `LSUIElement` accessory: it never becomes the active
/// application on its own, and the SwiftUI `MenuBarExtra` panel is only a
/// floating card. While EV is inactive, macOS delivers ⌘V (and every other
/// Edit shortcut) to whichever app was frontmost before the card opened,
/// so typing may land but paste cannot. While the panel is open for text
/// entry EV must therefore promote to `.regular`, become active, make the
/// panel key, and expose a standard Edit menu whose items travel the
/// responder chain to the focused field editor. Closing the panel restores
/// `.accessory` unless another foreground session (e.g. a TCC prompt held
/// by `AppForeground`) still needs `.regular`.
public enum PanelActivationPolicy {
    public struct EditCommand: Equatable, Sendable {
        public let title: String
        /// Responder-chain selector name, e.g. `"paste:"`.
        public let actionName: String
        public let keyEquivalent: String
        public let needsCommandModifier: Bool
        public let needsShiftModifier: Bool

        public init(
            title: String,
            actionName: String,
            keyEquivalent: String,
            needsCommandModifier: Bool = true,
            needsShiftModifier: Bool = false
        ) {
            self.title = title
            self.actionName = actionName
            self.keyEquivalent = keyEquivalent
            self.needsCommandModifier = needsCommandModifier
            self.needsShiftModifier = needsShiftModifier
        }
    }

    /// The standard Edit menu. Items must target the first responder
    /// (nil target) so the field editor of the focused text field
    /// receives cut/copy/paste.
    public static let editCommands: [EditCommand] = [
        EditCommand(title: "Undo", actionName: "undo:", keyEquivalent: "z"),
        EditCommand(title: "Redo", actionName: "redo:", keyEquivalent: "Z", needsShiftModifier: true),
        EditCommand(title: "Cut", actionName: "cut:", keyEquivalent: "x"),
        EditCommand(title: "Copy", actionName: "copy:", keyEquivalent: "c"),
        EditCommand(title: "Paste", actionName: "paste:", keyEquivalent: "v"),
        EditCommand(title: "Paste and Match Style", actionName: "pasteAsPlainText:", keyEquivalent: "V", needsShiftModifier: true),
        EditCommand(title: "Delete", actionName: "delete:", keyEquivalent: ""),
        EditCommand(title: "Select All", actionName: "selectAll:", keyEquivalent: "a"),
    ]

    public static func activationPolicy(
        panelOpen: Bool,
        otherForegroundSessions: Int
    ) -> NSApplication.ActivationPolicy {
        if panelOpen { return .regular }
        return otherForegroundSessions > 0 ? .regular : .accessory
    }

    public enum EditShortcut: Equatable, Sendable {
        case undo, redo, cut, copy, paste, selectAll
    }

    /// Decides whether a key event is an editing shortcut the panel
    /// handles directly, bypassing the main menu. Only ⌘-shortcuts aimed
    /// at a text responder qualify — everything else (including ⌘Q)
    /// keeps its normal routing.
    public static func editShortcut(
        key: String,
        commandDown: Bool,
        shiftDown: Bool,
        isTextResponder: Bool
    ) -> EditShortcut? {
        guard commandDown, isTextResponder else { return nil }
        switch key.lowercased() {
        case "v": return .paste
        case "c": return .copy
        case "x": return .cut
        case "a": return .selectAll
        case "z": return shiftDown ? .redo : .undo
        default: return nil
        }
    }

    /// The panel window is the visible, keyable window that is not the
    /// 1×1 off-screen keep-alive window.
    public static func isPanelCandidate(
        isVisible: Bool,
        canBecomeKey: Bool,
        width: CGFloat,
        isKeepAliveWindow: Bool
    ) -> Bool {
        isVisible && canBecomeKey && !isKeepAliveWindow && width > 50
    }
}
