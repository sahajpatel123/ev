import Combine
import Foundation

/// Observable owner of the presence snapshot. The HUD pill, confirm card,
/// and timeline render `snapshot` and nothing else; events arrive only via
/// an attached `PresenceTransport`.
@MainActor
public final class PresenceStore: ObservableObject {
    @Published public private(set) var snapshot = PresenceSnapshot()

    private var task: Task<Void, Never>?
    private var transport: PresenceTransport?

    public init() {}

    public func attach(_ transport: PresenceTransport) {
        detach()
        self.transport = transport
        task = Task { [weak self] in
            guard let self else { return }
            do {
                for try await item in transport.run() {
                    try Task.checkCancellation()
                    await MainActor.run { [weak self] in
                        self?.snapshot.apply(item)
                    }
                }
            } catch is CancellationError {
                // Detach is silent: the next attach re-announces state.
            } catch {
                await MainActor.run { [weak self] in
                    self?.snapshot.apply(
                        .connection(.offline(note: "Stream ended: \(error.localizedDescription)"))
                    )
                }
            }
        }
    }

    public func detach() {
        task?.cancel()
        task = nil
        transport?.stop()
        transport = nil
    }

    /// Test seam and app-local event entry (ambient.looking_at is produced
    /// on-device). Transport frames and local frames share one pipeline.
    public func apply(_ event: PresenceTransportEvent) {
        snapshot.apply(event)
    }

    /// Dismiss a terminal turn (completed/failed) from the pill. Live turns
    /// cannot be dismissed — only cancelled through the turn path.
    public func dismissTurn() {
        if snapshot.turn?.outcome != nil {
            snapshot.turn = nil
        }
    }
}
