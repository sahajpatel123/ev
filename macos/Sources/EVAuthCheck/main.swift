import EVAuth
import Foundation

// Drives the shipped APIAuthKey resolver (no XCTest on this host).
var failed = 0

func expect(_ cond: Bool, _ name: String) {
    if cond {
        print("ok: \(name)")
    } else {
        print("FAIL: \(name)")
        failed += 1
    }
}

expect(!APIAuthKey.isUsable(""), "empty rejected")
expect(!APIAuthKey.isUsable("dev"), "dev rejected")
expect(!APIAuthKey.isUsable("changeme"), "changeme rejected")
expect(!APIAuthKey.isUsable("earskey"), "short leftover rejected")
expect(APIAuthKey.isUsable(String(repeating: "a", count: 16)), "16-char accepted")
expect(APIAuthKey.isUsable(String(repeating: "b", count: 64)), "64-char accepted")

let master = String(repeating: "m", count: 64)
let resolved = APIAuthKey.resolve(
    environment: [:],
    fileValues: ["EV_API_KEY": "earskey", "EV_MASTER_KEY": master],
    defaultsKey: "changeme"
)
expect(resolved == master, "file master wins over short api key and stale defaults")

let noFile = APIAuthKey.resolve(environment: [:], fileValues: [:], defaultsKey: "changeme")
expect(noFile == "dev", "unusable defaults fall through to dev")

let envKey = String(repeating: "e", count: 32)
let envWins = APIAuthKey.resolve(
    environment: ["EV_API_KEY": envKey],
    fileValues: ["EV_MASTER_KEY": String(repeating: "f", count: 32)],
    defaultsKey: String(repeating: "d", count: 32)
)
expect(envWins == envKey, "usable environment key wins")

if failed > 0 {
    fputs("EVAuthCheck: \(failed) failed\n", stderr)
    exit(1)
}
print("EVAuthCheck: all passed")
