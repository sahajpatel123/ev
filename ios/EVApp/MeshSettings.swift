import EVClient
import SwiftUI

/// iPhone Evie Mesh settings: the BLE constellation toggle, the
/// proximity projection of every other device, converge beacons,
/// and the mesh's honest status card.
struct MeshSettingsView: View {
    @ObservedObject private var mesh = EvieMeshStore.shared
    @StateObject private var runner = MeshIntentRunner.shared
    private let client: EVAPIClient
    private let deviceId: String

    @State private var status: EvieMeshStatus?
    @State private var readout: EvieProximityReadout?
    @State private var error: String?
    @State private var lastAction = ""

    init(client: EVAPIClient, deviceId: String) {
        self.client = client
        self.deviceId = deviceId
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Evie Mesh").font(.title2.bold())
                Text(
                    "Your MacBook and both iPhones advertise over BLE and observe each other. " +
                    "Proximity decides where Evie answers — the device you are standing next to."
                )
                .font(.caption)
                .foregroundStyle(.secondary)

                Toggle("Mesh constellation", isOn: Binding(
                    get: { mesh.isMeshEnabled },
                    set: { enabled in
                        if enabled {
                            mesh.start(client: client, deviceId: deviceId, capabilities: ["mesh", "camera", "text", "sensor"])
                        } else {
                            mesh.stop()
                        }
                    }
                ))
                .padding(.vertical, 4)

                if let zones = readout?.zones {
                    HStack(spacing: 10) {
                        ZoneChip(name: "Immediate", count: zones["immediate"], color: .green)
                        ZoneChip(name: "Near", count: zones["near"], color: .mint)
                        ZoneChip(name: "Far", count: zones["far"], color: .yellow)
                        ZoneChip(name: "Edge", count: zones["edge"], color: .gray)
                    }
                }

                if !mesh.sightings.isEmpty {
                    Text("Seen nearby").font(.headline)
                    ForEach(mesh.sightings.prefix(6), id: \.bleIdentifier) { sighting in
                        HStack {
                            Image(systemName: "antenna.radiowaves.left.and.right")
                            VStack(alignment: .leading, spacing: 2) {
                                Text(sighting.subjectDeviceId.prefix(12)).font(.caption)
                                if let rssi = sighting.rssi {
                                    Text("RSSI \(Int(rssi)) dBm").font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Text(sighting.zone.rawValue)
                                .font(.caption2)
                                .padding(.horizontal, 6)
                                .background(Color.secondary.opacity(0.15), in: Capsule())
                        }
                    }
                }

                HStack {
                    Button {
                        Task { await converge() }
                    } label: {
                        Label("Converge", systemImage: "speaker.wave.2.fill")
                    }
                    Button {
                        Task { await capture() }
                    } label: {
                        Label("Ask capture", systemImage: "camera.fill")
                    }
                    Button {
                        Task { await refresh() }
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                }
                .buttonStyle(.bordered)

                if !runner.lastActivity.isEmpty {
                    Text(runner.lastActivity).font(.caption2).foregroundStyle(.secondary)
                }
                if let err = error {
                    Text(err).font(.caption2).foregroundStyle(.red)
                }

                if let status {
                    Divider()
                    Text("Mesh status").font(.headline)
                    Text("Service \(status.serviceUuid)").font(.caption2)
                    Text("Storage: \(status.storage)").font(.caption2).foregroundStyle(.secondary)
                    if !status.macVerbs.isEmpty {
                        Text("Mac verbs: \(status.macVerbs.keys.sorted().joined(separator: ", "))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding()
        }
        .task {
            await refresh()
        }
    }

    private func refresh() async {
        do {
            async let status = client.fetchMeshStatus()
            async let near = client.fetchProximity()
            self.status = try await status
            readout = try await near
            error = nil
        } catch {
            self.error = String(describing: error)
        }
    }

    private func converge() async {
        do {
            let response = try await client.converge(reason: "find my devices")
            lastAction = "converge sent to \(response.targetCount ?? 0) devices"
            await refresh()
        } catch {
            self.error = String(describing: error)
        }
    }

    private func capture() async {
        do {
            let response = try await client.photoCapture(reason: "requested from iPhone")
            lastAction = "capture intent \(response.intentId?.prefix(8) ?? "?") sent"
            await refresh()
        } catch {
            self.error = String(describing: error)
        }
    }
}

private struct ZoneChip: View {
    let name: String
    let count: Int?
    let color: Color

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text("\(name) \(count ?? 0)").font(.caption2)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Color.secondary.opacity(0.12), in: Capsule())
    }
}
