import AppKit
import EVClient
import EVUI
import SwiftUI

/// The menu-bar panel: status, always-available capture, chat, HUD card,
/// permissions, and launch-at-login.
struct MenuBarView: View {
    @EnvironmentObject private var model: AppModel
    @State private var chatDraft = ""
    @State private var showPermissions = false
    @AppStorage("ev.hasSeenLifeGrant") private var hasSeenLifeGrant = false

    var body: some View {
        Group {
            if showPermissions {
                PermissionsPanelView(onBack: { showPermissions = false })
            } else {
                homeContent
            }
        }
        .padding(12)
        .onAppear {
            model.start()
            if !hasSeenLifeGrant {
                showPermissions = true
                hasSeenLifeGrant = true
            }
        }
    }

    private var homeContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            statusHeader
            Divider()
            captureRow
            Divider()
            chatSection
            if let hud = model.hudCard {
                Divider()
                hudView(hud)
                if hud.isApprovalHold {
                    Button(model.confirmingHud ? "Confirming…" : "Confirm") {
                        model.confirmHudAction()
                    }
                    .disabled(model.confirmingHud)
                    .font(.caption)
                }
            }
            Divider()
            footerRow
        }
    }

    private var statusHeader: some View {
        HStack {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            Text("EVIE")
                .font(.headline)
            Text(model.status.label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            if model.isLivePaused {
                Text("paused")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            } else if model.isLiveMuted {
                Text("muted")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            } else if model.isLiveActive {
                Text("live")
                    .font(.caption2)
                    .foregroundStyle(.green)
            } else if model.isRecording {
                Text("REC")
                    .font(.caption2)
                    .foregroundStyle(.red)
            }
            Text("cam · \(model.cameraState.presentationLabel)")
                .font(.caption2)
                .foregroundStyle(model.cameraState.isTruthfullyActive ? .green : .secondary)
            if model.queueCount > 0 {
                Text("\(model.queueCount) queued")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
    }

    private var statusColor: Color {
        switch model.status {
        case .offline: return .gray
        case .listening: return .green
        case .thinking: return .blue
        case .speaking: return .purple
        }
    }

    private var captureRow: some View {
        HStack(spacing: 6) {
            TextField("Capture…", text: $model.captureText)
                .textFieldStyle(.roundedBorder)
                .onSubmit {
                    model.capture()
                }
            Button("Remember") {
                model.capture()
            }
        }
    }

    private var chatSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Conversation")
                .font(.caption)
                .foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(model.messages) { message in
                        Text("\(message.role): \(message.text)")
                            .font(.body)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if model.messages.isEmpty {
                        Text("Just talk — EV is listening. No need to say EVIE.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .frame(maxHeight: 180)

            HStack(spacing: 6) {
                TextField("Ask EVIE…", text: $chatDraft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit {
                        let text = chatDraft
                        chatDraft = ""
                        model.sendChat(text)
                    }
                Button("Send") {
                    let text = chatDraft
                    chatDraft = ""
                    model.sendChat(text)
                }
            }

            HStack(spacing: 8) {
                Spacer()
                Button(liveButtonTitle) {
                    model.toggleTalk()
                }
                .font(.caption)
                Button(cameraButtonTitle) {
                    model.toggleCamera()
                }
                .disabled(model.cameraRequestInFlight || model.cameraState.state == .denied)
                .font(.caption)
            }
            if let error = model.lastError {
                Text(error)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var footerRow: some View {
        HStack {
            Toggle("Open at login", isOn: launchAtLoginBinding)
                .font(.caption)
                .toggleStyle(.checkbox)
            Spacer()
            if PresenceController.shared.lastContent != nil {
                Button("Last lookouts") {
                    PresenceController.shared.showLast()
                }
                .font(.caption)
            }
            Button("Permissions") {
                showPermissions = true
            }
            .font(.caption)
            Button("Quit") {
                AppLifecycle.quit()
            }
            .font(.caption)
        }
    }

    private var liveButtonTitle: String {
        if model.isLiveActive || model.isLiveMuted {
            return model.isLiveMuted ? "Unmute" : "Mute"
        }
        return model.isRecording ? "Stop & send" : "Push to talk"
    }

    private var cameraButtonTitle: String {
        if model.cameraRequestInFlight { return "Camera…" }
        if model.cameraState.isTruthfullyActive { return "Camera off" }
        if model.cameraState.state == .denied { return "Camera denied" }
        return "Camera on"
    }

    private var developerStatusSurface: some View {
        GroupBox("Developer status") {
            VStack(alignment: .leading, spacing: 4) {
                statusRow("Backend", backendURLText)
                statusRow("Process", backendProcessText)
                statusRow("Live", liveProviderModelText)
                statusRow("IDs", deviceSummary)
                statusRow("Advertised", advertisedToolText)
                statusRow("Provider ack", providerAcknowledgementText)
                statusRow("Capability", capabilitySummary, color: capabilityStatusColor)
                statusRow("Tool call", toolCallText)
                statusRow("Progress", toolProgressText, color: toolProgressColor)
                statusRow("Tool result", toolResultText, color: toolResultColor)
                statusRow("Evidence", evidenceText)
                if let hold = model.liveConfirmationHold {
                    statusRow("Hold action", safeStatusText(hold.action, fallback: "not reported"))
                    statusRow("Hold target", safeStatusText(hold.target, fallback: "not reported"))
                    statusRow("Hold method", safeStatusText(hold.method, fallback: "not reported"))
                    statusRow("Hold expiry", safeStatusText(hold.expiry, fallback: "not reported"), color: .orange)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Developer live status")
    }

    @ViewBuilder
    private func hudView(_ card: HUDCard) -> some View {
        let toolKinds = ["progress", "approval_hold", "evidence", "tool_result"]
        if toolKinds.contains(card.metaKind ?? "") {
            VStack(alignment: .leading, spacing: 4) {
                Text("Tool status")
                    .font(.headline)
                Text("Tool: \(safeStatusText(card.meta?["tool"]?.stringValue, fallback: "not reported"))")
                    .font(.caption)
                Text("Payload content withheld; see developer status for lifecycle metadata.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding()
            .background(Color.gray.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        } else {
            HUDCardView(card: card)
                .padding(.vertical, 2)
        }
    }

    private func statusRow(_ label: String, _ value: String, color: Color = .secondary) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .frame(width: 78, alignment: .leading)
            Text(value)
                .font(.caption2.monospaced())
                .foregroundStyle(color)
                .lineLimit(2)
                .truncationMode(.middle)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var backendURLText: String {
        guard var components = URLComponents(url: model.config.baseURL, resolvingAgainstBaseURL: false) else {
            return "not reported"
        }
        // A backend URL is useful developer context, but query items and user
        // info are not status metadata and may contain credentials.
        components.user = nil
        components.password = nil
        components.query = nil
        components.fragment = nil
        let source = model.liveRuntimeDiagnostics.backendURLSource.map { " [\($0)]" } ?? ""
        return safeStatusText((components.string ?? "not reported") + source, fallback: "not reported")
    }

    private var backendProcessText: String {
        let diagnostics = model.liveRuntimeDiagnostics
        let pid = diagnostics.backendPID.map(String.init) ?? "not reported"
        let version = safeStatusText(diagnostics.backendVersion, fallback: "not reported")
        let started = safeStatusText(diagnostics.backendStartedAt, fallback: "not reported")
        let fingerprint = safeStatusText(
            diagnostics.backendSourceFingerprint,
            fallback: "fingerprint not reported"
        )
        return "pid \(pid) · version \(version) · started \(started) · \(fingerprint)"
    }

    private var liveProviderModelText: String {
        let provider = safeStatusText(
            model.activeLiveProvider ?? model.liveRuntimeDiagnostics.provider,
            fallback: "not reported"
        )
        let liveModel = safeStatusText(
            model.activeLiveModel ?? model.liveRuntimeDiagnostics.model,
            fallback: "not reported"
        )
        return "provider \(provider) · model \(liveModel)"
    }

    private var deviceSummary: String {
        let session = safeID(model.sessionId)
        let device = safeID(model.liveRuntimeDiagnostics.deviceID ?? model.config.deviceID)
        let tts = model.liveTTSDeviceID.map { " · tts \(safeID($0))" } ?? ""
        return "session \(session) · device \(device)\(tts)"
    }

    private var advertisedToolText: String {
        let names = model.liveToolsReported
            ? model.advertisedLiveToolNames
            : model.liveRuntimeDiagnostics.advertisedTools
        guard model.liveToolsReported || !names.isEmpty || model.providerToolsReported else {
            return "not reported"
        }
        guard !names.isEmpty else {
            return "none advertised · fail-closed"
        }
        return compactToolList(names)
    }

    private var providerAcknowledgementText: String {
        let names = model.providerToolsReported
            ? model.providerAcknowledgedToolNames
            : model.liveRuntimeDiagnostics.providerAcknowledgedTools
        let reported = model.providerToolsReported || model.liveRuntimeDiagnostics.providerSessionReady
        guard reported else { return "not reported" }
        if names.isEmpty {
            return (model.providerSessionReady || model.liveRuntimeDiagnostics.providerSessionReady)
                ? "none acknowledged"
                : "awaiting acknowledgement"
        }
        return compactToolList(names)
    }

    private var capabilitySummary: String {
        let error = model.capabilityError ?? model.liveRuntimeDiagnostics.capabilityErrors.first
        if let error, !error.isEmpty {
            return "error · \(safeStatusText(error, fallback: "reported"))"
        }
        if !model.capabilityManifest.isEmpty {
            return "manifest reported · \(model.capabilityManifest.enabled.count) enabled"
        }
        if model.liveToolsReported {
            return "no capability manifest reported"
        }
        return "not reported"
    }

    private var capabilityStatusColor: Color {
        (model.capabilityError == nil && model.liveRuntimeDiagnostics.capabilityErrors.isEmpty)
            ? .secondary
            : .orange
    }

    private var toolCallText: String {
        safeStatusText(model.lastToolCallName, fallback: "none reported")
    }

    private var toolProgressText: String {
        guard let status = model.lastToolResultStatus else { return "no active tool" }
        if status == "in progress" { return "running \(toolCallText)" }
        if status == "waiting for confirmation" { return "waiting \(toolCallText)" }
        return "idle"
    }

    private var toolProgressColor: Color {
        model.lastToolResultStatus == "in progress" ? .blue : .secondary
    }

    private var toolResultText: String {
        safeStatusText(model.lastToolResultStatus, fallback: "none reported")
    }

    private var toolResultColor: Color {
        switch model.lastToolResultStatus {
        case "success", "success · evidence reported": return .green
        case "failed": return .orange
        default: return .secondary
        }
    }

    private var evidenceText: String {
        guard let timestamp = model.lastToolEvidenceTimestamp else { return "none reported" }
        let source = safeStatusText(model.lastToolEvidenceSource, fallback: "source not reported")
        return "\(safeStatusText(timestamp)) · \(source)"
    }

    private func compactToolList(_ names: [String]) -> String {
        names
            .map { safeStatusText($0, fallback: "unnamed tool") }
            .joined(separator: ", ")
    }

    private func safeID(_ value: String?) -> String {
        guard let value, !value.isEmpty else { return "not reported" }
        let compact = value.replacingOccurrences(of: "\n", with: " ")
        guard compact.count > 28 else { return compact }
        return "\(compact.prefix(12))…\(compact.suffix(12))"
    }

    private func safeStatusText(_ value: String?, fallback: String = "not reported", maxLength: Int = 180) -> String {
        guard var value, !value.isEmpty else { return fallback }
        let lowered = value.lowercased()
        let secretMarkers = [
            "api_key", "apikey", "access_token", "refresh_token", "authorization:",
            "bearer ", "client_secret", "private_key", "x-api-key", "token="
        ]
        if secretMarkers.contains(where: lowered.contains) {
            return "[redacted]"
        }
        if let queryStart = value.firstIndex(of: "?") {
            value = String(value[..<queryStart])
        }
        value = value.replacingOccurrences(of: "\n", with: " ")
        if value.count > maxLength {
            return "\(value.prefix(maxLength))…"
        }
        return value
    }

    private var launchAtLoginBinding: Binding<Bool> {
        Binding(
            get: { model.launchAtLogin },
            set: { model.launchAtLogin = $0 }
        )
    }
}
