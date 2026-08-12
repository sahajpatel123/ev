import Foundation
import EVClient
import EVUI
import SwiftUI

let client = EVAPIClient(
    baseURL: URL(string: "https://ev.test")!,
    token: "test"
)
let queue = OfflineCaptureQueue(store: MemoryCaptureQueueStore())
let card = HUDCard(
    schemaVersion: "ev.hud.card.v1",
    generatedAt: "2026-08-09T12:00:00Z",
    title: "EV status",
    body: "No active signals. EV is watching.",
    priority: 0.0
)

_ = HUDCardView(card: card)
_ = TodayView(client: client)
_ = CaptureView(client: client, queue: queue)
_ = MemoryBrowserView(client: client)
_ = ConversationView(client: client)
_ = VoiceCaptureView(client: client, deviceId: "mac-shell")
_ = QueueIndicatorView(queue: queue)
_ = AppShellView(client: client, queue: queue)

print("EVUIValidate: all shared views constructed (macOS build)")
