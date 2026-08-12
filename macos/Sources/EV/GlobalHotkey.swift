import AppKit

/// Global hotkey using an `NSEvent` global key-down monitor.
///
/// Global monitors only receive other apps' key events when the process is
/// trusted for Accessibility, which is exactly the permission contract the
/// SUIT app exposes in its Permissions panel: without it the hotkey silently
/// does nothing, so we surface it loudly instead.
final class GlobalHotkey {
    private var monitor: Any?

    func start(
        keyCode: UInt16,
        flags: NSEvent.ModifierFlags,
        handler: @escaping () -> Void
    ) {
        stop()
        monitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { event in
            let pressed = event.modifierFlags.intersection([.command, .option, .control, .shift])
            guard event.keyCode == keyCode, pressed == flags else { return }
            DispatchQueue.main.async(execute: handler)
        }
    }

    func stop() {
        if let monitor {
            NSEvent.removeMonitor(monitor)
            self.monitor = nil
        }
    }
}
