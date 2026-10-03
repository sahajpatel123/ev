import Combine
import EVClient
import Foundation

/// Cross-device Follow-Me surface for the Mac: presence HUD state, clipboard
/// constellation, intent composer, pocket-executive trigger. All state is
/// server-projected; this class owns no canonical truth.
@MainActor
final class FollowMeModel: ObservableObject {
    @Published var hud: EviePresenceHUD?
    @Published var primary: EviePresenceDevice?
    @Published var clipboardItems: [EvieClipboardItem] = []
    @Published var pendingIntents: [EvieIntent] = []
    @Published var lastError: String?

    private let client: EVAPIClient
    private var pollTask: Task<Void, Never>?

    init(client: EVAPIClient) {
        self.client = client
    }

    func startPolling(interval: TimeInterval = 5) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh() async {
        do {
            async let hud = client.fetchPresenceHUD()
            async let clip = client.listClipboard()
            async let intents = client.listIntents(status: "PENDING")
            async let primary = client.fetchPrimaryDevice()
            let (h, c, i, p) = try await (hud, clip, intents, primary)
            self.hud = h
            self.clipboardItems = c.items
            self.pendingIntents = i.intents
            self.primary = p.primary
            self.lastError = nil
        } catch {
            self.lastError = String(describing: error)
        }
    }

    func sendFocusHandoff(to primary: Bool = true) async {
        do {
            _ = try await client.publishIntent(kind: "focus.handoff", args: [:])
            await refresh()
        } catch {
            lastError = String(describing: error)
        }
    }

    func pushClipboard(_ text: String) async {
        do {
            _ = try await client.pushClipboard(text: text)
            await refresh()
        } catch {
            lastError = String(describing: error)
        }
    }

    func headingOut() async {
        do {
            _ = try await client.headingOut()
            await refresh()
        } catch {
            lastError = String(describing: error)
        }
    }

    func askLook(reason: String, kind: String) async {
        do {
            _ = try await client.requestLook(reason: reason, kind: kind)
            await refresh()
        } catch {
            lastError = String(describing: error)
        }
    }
}
