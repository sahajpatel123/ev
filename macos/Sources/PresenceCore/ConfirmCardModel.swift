import Combine
import Foundation

/// Confirm-card decision state machine. The physical paths (`approve()` from
/// Return/click) and the voice path (`approveSpoken()`) are separate methods
/// so the HIGH_RISK spoken rule is enforced by construction, not by flag.
@MainActor
public final class ConfirmCardModel: ObservableObject {
    public enum Phase: Sendable, Equatable {
        case pending
        case working
        case approved(status: String)
        case denied(status: String)
        case failed(message: String)
    }

    @Published public private(set) var phase: Phase = .pending
    @Published public private(set) var now = Date()

    public let ticket: NeedsConfirm
    private let client: ConfirmClient
    private var clock: Timer?

    public init(ticket: NeedsConfirm, client: ConfirmClient) {
        self.ticket = ticket
        self.client = client
    }

    public var expired: Bool {
        ConfirmPolicy.isExpired(expiresAt: ticket.expiresAt, now: now)
    }

    public var approveEnabled: Bool {
        phase == .pending && !expired && ConfirmPolicy.physicalMayApprove(ticket)
    }

    public var denyEnabled: Bool {
        phase == .pending && !expired
    }

    /// Live countdown tick while the card is shown. Tests set `now` via
    /// `setNow` instead of waiting on wall time.
    public func startClock() {
        stopClock()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = Date() }
        }
        clock = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    public func stopClock() {
        clock?.invalidate()
        clock = nil
    }

    public func setNow(_ date: Date) {
        now = date
    }

    // MARK: - Physical paths (Return / click)

    public func approve() async {
        guard phase == .pending else { return }
        guard !expired else {
            phase = .failed(message: "This request expired — nothing was approved.")
            return
        }
        guard ConfirmPolicy.physicalMayApprove(ticket) else {
            phase = .failed(message: "Details unavailable — approve in the terminal instead. Nothing was approved.")
            return
        }
        phase = .working
        do {
            // Master-key callers approve with no factor; device-token
            // callers get 403 once, then mint exactly one reverify token
            // and retry once. No silent second attempts after that.
            do {
                let decision = try await client.approve(
                    ticketId: ticket.ticketId,
                    path: ticket.approve?.path ?? "",
                    reason: ConfirmRequest.approveReason,
                    reverify: nil
                )
                phase = .approved(status: decision.status)
            } catch let error as ConfirmError {
                switch error {
                case .needsReverify:
                    let token = try await client.mintReverify()
                    let decision = try await client.approve(
                        ticketId: ticket.ticketId,
                        path: ticket.approve?.path ?? "",
                        reason: ConfirmRequest.approveReason,
                        reverify: token
                    )
                    phase = .approved(status: decision.status)
                default:
                    throw error
                }
            }
        } catch is CancellationError {
            phase = .pending
        } catch let error as ConfirmError {
            phase = .failed(message: error.message)
        } catch {
            phase = .failed(message: "Approve failed: \(error.localizedDescription)")
        }
    }

    public func deny() async {
        guard phase == .pending else { return }
        guard !expired else {
            phase = .failed(message: "This request already expired.")
            return
        }
        phase = .working
        do {
            let decision = try await client.deny(
                ticketId: ticket.ticketId,
                path: ticket.deny?.path ?? "",
                reason: ConfirmRequest.denyReason
            )
            phase = .denied(status: decision.status)
        } catch is CancellationError {
            phase = .pending
        } catch let error as ConfirmError {
            phase = .failed(message: error.message)
        } catch {
            phase = .failed(message: "Deny failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Voice path (spoken "yes")

    /// The ONLY entry the voice loop may call. HIGH_RISK and first-contact
    /// tickets are refused without touching the network; low-risk tickets
    /// proceed through the same approve flow as the physical path.
    public func approveSpoken() async {
        guard ConfirmPolicy.spokenMayApprove(ticket) else {
            phase = .failed(message: "A spoken yes can't approve this — confirm on the card instead. Nothing was approved.")
            return
        }
        await approve()
    }
}
