import EVClient
import SwiftUI

/// Mac menubar tab for the Follow-Me layer: presence HUD, primary device,
/// clipboard constellation, the creative cross-device actions — and the
/// Evie Mesh: proximity constellation, converge beacons, remote capture,
/// and approval-gated Mac verbs.
struct FollowMeView: View {
    @StateObject private var model: FollowMeModel
    private let client: EVAPIClient
    private let meshDeviceId: String

    @State private var meshStatus: EvieMeshStatus?
    @State private var proximity: EvieProximityReadout?
    @State private var approvals: [MeshApprovalRequest] = []
    @State private var meshLast: String = ""
    @State private var meshError: String?

    init(client: EVAPIClient) {
        _model = StateObject(wrappedValue: FollowMeModel(client: client))
        self.client = client
        meshDeviceId = UserDefaults.standard.string(forKey: "EV_DEVICE_ID")
            ?? EVClientAppConfig().deviceID
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Follow-Me").font(.headline)
            if let primary = model.primary {
                Label("Primary: \(primary.display_name) (\(primary.presence_state))", systemImage: "star.fill")
                    .font(.caption)
            }
            if let hud = model.hud {
                ForEach(hud.devices) { device in
                    HStack {
                        Circle().fill(device.presence_state == "ONLINE" ? Color.green : Color.gray).frame(width: 8, height: 8)
                        Text(device.display_name)
                        Spacer()
                        Text(device.presence_state).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Divider()
            HStack {
                Button("Heading out") { Task { await model.headingOut() } }
                Button("Hand off focus") { Task { await model.sendFocusHandoff() } }
                Button("Look (photo)") { Task { await model.askLook(reason: "owner asked", kind: "photo") } }
            }
            Divider()
            Text("Evie Mesh").font(.headline)
            if let zones = proximity?.zones {
                HStack(spacing: 8) {
                    MeshZonePill(name: "Immediate", count: zones["immediate"])
                    MeshZonePill(name: "Near", count: zones["near"])
                    MeshZonePill(name: "Far", count: zones["far"])
                    MeshZonePill(name: "Edge", count: zones["edge"])
                }
                .font(.caption2)
            }
            if let advertisements = meshStatus?.advertisements, !advertisements.isEmpty {
                ForEach(Array(advertisements.values.prefix(4)), id: \.deviceId) { advertisement in
                    HStack {
                        Image(systemName: "antenna.radiowaves.left.and.right")
                        Text(advertisement.deviceId.prefix(8))
                        Spacer()
                        if let battery = advertisement.batteryPercent {
                            Text("\(Int(battery))%").font(.caption).foregroundStyle(.secondary)
                        }
                        if advertisement.lowPower {
                            Text("low power").font(.caption2).foregroundStyle(.orange)
                        }
                    }
                    .font(.caption)
                }
            }
            HStack {
                Button("Converge") { Task { await converge() } }
                Button("Capture here") { Task { await captureHere() } }
                Button("Refresh mesh") { Task { await refreshMesh() } }
            }
            if !approvals.isEmpty {
                Divider()
                Text("Approvals").font(.headline)
                ForEach(approvals) { request in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(request.verb)").font(.caption)
                            Text("from \(request.requestedBy)").font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Allow") { Task { await MeshIntentPoller.shared.approve(request.intentId) ; await refreshMesh() } }
                            .buttonStyle(.borderedProminent)
                        Button("Deny") { Task { await MeshIntentPoller.shared.deny(request.intentId) ; await refreshMesh() } }
                            .buttonStyle(.bordered)
                    }
                    .padding(.vertical, 2)
                }
            }
            if !meshLast.isEmpty {
                Text(meshLast).font(.caption2).foregroundStyle(.secondary)
            }
            if let err = meshError ?? model.lastError {
                Text(err).font(.caption2).foregroundStyle(.red)
            }
        }
        .padding()
        .onAppear {
            model.startPolling()
            Task { await refreshMesh() }
        }
        .onDisappear { model.stopPolling() }
    }

    private func refreshMesh() async {
        do {
            async let status = client.fetchMeshStatus()
            async let near = client.fetchProximity()
            meshStatus = try await status
            proximity = try await near
            approvals = MeshIntentPoller.shared.pendingApprovals()
            meshError = nil
        } catch {
            meshError = String(describing: error)
        }
    }

    private func converge() async {
        do {
            let response = try await client.converge(reason: "menubar converge")
            meshLast = "converge: \(response.targetCount ?? 0) targets"
            await refreshMesh()
        } catch {
            meshError = String(describing: error)
        }
    }

    private func captureHere() async {
        // This Mac volunteers as the capture device: it posts the
        // intent itself, then its own poller executes it.
        do {
            let response = try await client.photoCapture(reason: "captured from menubar")
            meshLast = "capture intent: \(response.intentId ?? "?")"
            await refreshMesh()
        } catch {
            meshError = String(describing: error)
        }
    }
}

private struct MeshZonePill: View {
    let name: String
    let count: Int?

    var body: some View {
        HStack(spacing: 2) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text("\(name) \(count ?? 0)")
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Color.secondary.opacity(0.15), in: Capsule())
    }

    private var color: Color {
        switch name {
        case "Immediate": return .green
        case "Near": return .mint
        case "Far": return .yellow
        default: return .gray
        }
    }
}
