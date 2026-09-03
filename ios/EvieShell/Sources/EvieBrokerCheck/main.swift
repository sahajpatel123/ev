import EvieNativeBroker
import Foundation

@main
struct EvieBrokerCheck {
    static func main() {
        var failed = 0
        func check(_ name: String, _ ok: Bool) {
            if ok {
                print("PASS \(name)")
            } else {
                print("FAIL \(name)")
                failed += 1
            }
        }

        check("instagram-alias", AppLaunchRegistry.resolve("Insta")?.appID == "instagram")
        check("spotify", AppLaunchRegistry.resolve("Spotify")?.appID == "spotify")
        check("unknown-app", AppLaunchRegistry.resolve("BankOfNowhere") == nil)

        check("origin-localhost", TrustedOrigin.allows(URL(string: "http://127.0.0.1:8000/evie/")!))
        check("origin-tsnet", TrustedOrigin.allows(URL(string: "https://home.example.ts.net/evie/")!))
        check("origin-reject-external", !TrustedOrigin.allows(URL(string: "https://evil.example/evie/")!))

        check("reject-selector", NativeBridgeRequest.parse(["type": "invokeSelector"]) == nil)
        check("allow-haptic", NativeBridgeRequest.parse(["type": "haptic", "event": "selection"]) != nil)
        check("allow-pending-capture", NativeBridgeRequest.parse(["type": "pending_capture"]) != nil)
        check("allow-healthkit-snapshot", NativeBridgeRequest.parse(["type": "healthkit_snapshot"]) != nil)
        check("allow-calendar-snapshot", NativeBridgeRequest.parse(["type": "calendar_snapshot"]) != nil)
        check("allow-contacts-snapshot", NativeBridgeRequest.parse(["type": "contacts_snapshot"]) != nil)
        check("allow-notification-status", NativeBridgeRequest.parse(["type": "notification_status"]) != nil)
        check("allow-permission-status", NativeBridgeRequest.parse(["type": "permissionStatus"]) != nil)
        check("reject-eval", NativeBridgeRequest.parse(["type": "eval"]) == nil)

        let receipt = NativeReceipt(
            actionID: "ma_1",
            accepted: true,
            executed: false,
            verified: false,
            systemUIPresented: true,
            systemConfirmationRequired: true,
            result: "SYSTEM_UI_OPENED"
        )
        check("accepted-not-executed", receipt.accepted && !receipt.executed)
        check("executed-not-verified", !(receipt.executed && receipt.verified && receipt.result == "SYSTEM_UI_OPENED"))
        check("broker-version", BrokerVersion.version == "1.0.0")

        if failed > 0 {
            fputs("EvieBrokerCheck failed \(failed) assertion(s)\n", stderr)
            exit(1)
        }
        print("EvieBrokerCheck OK")
    }
}
