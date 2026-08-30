import AppKit
import SwiftUI

struct PermissionRowView: View {
    let status: PermissionStatus
    let onRequest: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(stateColor)
                .frame(width: 8, height: 8)
                .padding(.top, 4)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(status.kind.title)
                        .font(.caption)
                        .fontWeight(.semibold)
                    Text(stateLabel)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Text(status.whatBreaks)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                actionRow
                if let reset = status.resetCommand {
                    resetHint(command: reset)
                }
            }
            Spacer()
        }
    }

    @ViewBuilder
    private var actionRow: some View {
        HStack(spacing: 6) {
            if status.kind == .fullDiskAccess {
                Button("Reveal EV.app") {
                    PermissionCenter.revealAppInFinder()
                }
                .font(.caption2)
                Button("Open Settings") {
                    PermissionCenter.openSettings(for: .fullDiskAccess)
                }
                .font(.caption2)
            } else {
                switch status.state {
                case .notDetermined:
                    Button("Ask", action: onRequest)
                        .font(.caption2)
                case .denied, .restricted:
                    Button("Open Settings") {
                        PermissionCenter.openSettings(for: status.kind)
                    }
                    .font(.caption2)
                case .granted:
                    EmptyView()
                }
            }
        }
    }

    private func resetHint(command: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("EV is already in this list with its switch off. Flip it, or re-arm the prompt with:")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Text(command)
                    .font(.system(.caption2, design: .monospaced))
                    .textSelection(.enabled)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    _ = NSPasteboard.general.setString(command, forType: .string)
                }
                .font(.caption2)
            }
        }
    }

    private var stateLabel: String {
        switch status.state {
        case .granted: return "granted"
        case .denied: return "denied"
        case .notDetermined: return "not asked"
        case .restricted: return "restricted"
        }
    }

    private var stateColor: Color {
        switch status.state {
        case .granted: return .green
        case .denied: return .red
        case .notDetermined: return .orange
        case .restricted: return .red
        }
    }
}

struct PermissionsPanelView: View {
    @State private var statuses: [PermissionStatus] = []
    @State private var facts: [PermissionFact] = []
    @State private var isRequesting = false
    @State private var showDiagnostics = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            if statuses.isEmpty {
                ProgressView()
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(statuses, id: \.kind) { status in
                            PermissionRowView(status: status) {
                                request(status.kind)
                            }
                        }
                        Divider()
                        diagnosticsSection
                    }
                }
            }
        }
        .padding()
        .frame(width: 440, height: 560)
        .task {
            await refresh()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Permissions")
                .font(.headline)
            Text("macOS only lists EV in a Privacy pane after EV has asked for that service. Grant permissions brings EV to the foreground (menu-bar apps otherwise swallow the dialogs) and asks every service below. Answer each prompt first — opening Settings before that shows an empty list.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Grant permissions") {
                    isRequesting = true
                    Task {
                        statuses = await PermissionCenter.requestAll()
                        facts = PermissionCenter.diagnostics()
                        isRequesting = false
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(isRequesting)
                Button("Refresh") {
                    Task { await refresh() }
                }
                .font(.caption)
                if isRequesting {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
    }

    private var diagnosticsSection: some View {
        DisclosureGroup("Diagnostics", isExpanded: $showDiagnostics) {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(facts) { fact in
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(alignment: .top, spacing: 4) {
                            Text(fact.ok ? "✅" : "⚠️")
                                .font(.caption2)
                            Text("\(fact.title): \(fact.detail)")
                                .font(.caption2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let fix = fact.fix {
                            Text(fix)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.leading, 16)
                        }
                    }
                }
            }
            .padding(.top, 4)
        }
        .font(.caption)
    }

    private func request(_ kind: PermissionKind) {
        isRequesting = true
        Task {
            await PermissionCenter.request(kind)
            await refresh()
            isRequesting = false
        }
    }

    private func refresh() async {
        statuses = await PermissionCenter.statuses()
        facts = PermissionCenter.diagnostics()
    }
}

/// A real window, not a sheet on the menu-bar extra. TCC dialogs attach to the
/// active app; if the extra closes (or never activates), the dialog is
/// discarded and System Settings never lists EV.
@MainActor
enum PermissionsWindow {
    private static let anchor = PermissionsWindowAnchor()

    static func show() {
        anchor.show()
    }
}

private final class PermissionsWindowAnchor: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private var foregroundHeld = false

    @MainActor
    func show() {
        if let window, window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate()
            return
        }
        if !foregroundHeld {
            AppForeground.begin()
            foregroundHeld = true
        }
        let hosting = NSHostingController(rootView: PermissionsPanelView())
        let window = NSWindow(contentViewController: hosting)
        window.title = "EV Permissions"
        window.styleMask = [.titled, .closable]
        window.setContentSize(NSSize(width: 460, height: 580))
        window.level = .floating
        window.isReleasedWhenClosed = true
        window.delegate = self
        window.center()
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func windowWillClose(_ notification: Notification) {
        Task { @MainActor in
            if self.foregroundHeld {
                AppForeground.end()
                self.foregroundHeld = false
            }
            self.window = nil
        }
    }
}
