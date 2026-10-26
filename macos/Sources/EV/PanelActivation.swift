import AppKit
import EVRuntime

/// Turns the floating `MenuBarExtra` card into a real, active text-entry
/// surface while it is open, and returns EV to a menu-bar accessory when
/// it closes.
///
/// Three independent layers make text editing work, because each single
/// mechanism has a failure mode in a `MenuBarExtra` panel:
///
/// 1. Activation — EV promotes to `.regular` and the panel becomes key,
///    so macOS considers Evie the active application.
/// 2. Menu — a standard Edit menu is (re)installed when the panel opens.
///    SwiftUI manages `NSApp.mainMenu` and can replace the menu installed
///    at launch, so presence is re-asserted here, at the moment of use.
/// 3. Direct shortcuts — a local key monitor performs ⌘V/⌘C/⌘X/⌘A/⌘Z on
///    the field editor itself, with no menu involvement at all. This is
///    the layer that cannot be defeated by menu replacement or by the
///    panel's key-window behaviour.
@MainActor
enum PanelActivation {
    private static var keyMonitor: Any?

    static func begin() {
        AppForeground.begin()
        ensureEditMenu()
        installKeyMonitor()
        makePanelKey()
        // SwiftUI finishes presenting the panel after `onAppear`, and can
        // hand key status back to the status-bar window in the same run
        // loop turn. Re-assert once presentation has settled.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
            ensureEditMenu()
            makePanelKey()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            ensureEditMenu()
            makePanelKey()
        }
    }

    static func end() {
        removeKeyMonitor()
        AppForeground.end()
    }

    static func makePanelKey() {
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        NSRunningApplication.current.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        guard let panel = panelWindow() else { return }
        panel.makeKeyAndOrderFront(nil)
    }

    /// Rebuilds the main menu if the Paste item is not currently present.
    static func ensureEditMenu() {
        if menuHasPaste(NSApp.mainMenu) { return }
        (NSApp.delegate as? AppDelegate)?.installAppMenu()
    }

    private static func menuHasPaste(_ menu: NSMenu?) -> Bool {
        guard let menu else { return false }
        for item in menu.items {
            if item.action == NSSelectorFromString("paste:") { return true }
            if let submenu = item.submenu, menuHasPaste(submenu) { return true }
        }
        return false
    }

    // MARK: - Direct editing shortcuts

    private static func installKeyMonitor() {
        removeKeyMonitor()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            MainActor.assumeIsolated {
                handleKeyDown(event) ? nil : event
            }
        }
    }

    private static func removeKeyMonitor() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
    }

    /// Returns true when the event was consumed as an editing shortcut.
    private static func handleKeyDown(_ event: NSEvent) -> Bool {
        guard let panel = panelWindow(), NSApp.keyWindow === panel else { return false }
        guard let editor = panel.firstResponder as? NSText else { return false }
        let shortcut = PanelActivationPolicy.editShortcut(
            key: event.charactersIgnoringModifiers ?? "",
            commandDown: event.modifierFlags.contains(.command),
            shiftDown: event.modifierFlags.contains(.shift),
            isTextResponder: true
        )
        guard let shortcut else { return false }
        return perform(shortcut, on: editor)
    }

    private static func perform(_ shortcut: PanelActivationPolicy.EditShortcut, on editor: NSText) -> Bool {
        switch shortcut {
        case .paste:
            guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else {
                return false
            }
            if let textView = editor as? NSTextView {
                textView.insertText(text, replacementRange: textView.selectedRange())
            } else {
                editor.insertText(text)
            }
            return true
        case .copy:
            (editor as? NSTextView)?.copy(nil)
            return editor is NSTextView
        case .cut:
            (editor as? NSTextView)?.cut(nil)
            return editor is NSTextView
        case .selectAll:
            editor.selectAll(nil)
            return true
        case .undo:
            editor.undoManager?.undo()
            return true
        case .redo:
            editor.undoManager?.redo()
            return true
        }
    }

    private static func panelWindow() -> NSWindow? {
        NSApp.windows
            .filter { window in
                PanelActivationPolicy.isPanelCandidate(
                    isVisible: window.isVisible,
                    canBecomeKey: window.canBecomeKey,
                    width: window.frame.width,
                    // The keep-alive window is 1×1 at (-10000, -10000).
                    isKeepAliveWindow: window.frame.origin.x <= -9_000
                )
            }
            .max { $0.frame.width < $1.frame.width }
    }
}
