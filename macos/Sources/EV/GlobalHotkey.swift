import AppKit

/// Global hotkey using an `NSEvent` global key-down monitor.
///
/// Global monitors only receive other apps' key events when the process is
/// trusted for Accessibility, which is exactly the permission contract the
/// SUIT app exposes in its Permissions panel: without it the hotkey silently
/// does nothing, so we surface it loudly instead.
final class GlobalHotkey {
    private var globalMonitor: Any?
    private var localMonitor: Any?

    func start(
        keyCode: UInt16,
        flags: NSEvent.ModifierFlags,
        handler: @escaping () -> Void
    ) {
        stop()
        let matches: (NSEvent) -> Bool = { event in
            let pressed = event.modifierFlags.intersection([.command, .option, .control, .shift])
            return event.keyCode == keyCode && pressed == flags
        }
        // Other apps: requires Accessibility. EV-is-key: local monitor
        // (global monitors never see this process's key-downs).
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { event in
            guard matches(event) else { return }
            DispatchQueue.main.async(execute: handler)
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard matches(event) else { return event }
            DispatchQueue.main.async(execute: handler)
            return nil
        }
    }

    func stop() {
        if let globalMonitor {
            NSEvent.removeMonitor(globalMonitor)
            self.globalMonitor = nil
        }
        if let localMonitor {
            NSEvent.removeMonitor(localMonitor)
            self.localMonitor = nil
        }
    }
}
