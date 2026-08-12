import AppKit
import Foundation

/// EVNotificationHelper — the delivery shim Agent 14's PULSE backend calls.
///
/// The helper never posts notifications itself. It opens `ev://notification`
/// on the bundled EV.app (via its registered URL scheme); the app is the only
/// component that calls UNUserNotificationCenter, which keeps exactly one
/// native delivery path and gives notifications a real bundle identity.

let arguments = CommandLine.arguments

func value(for name: String) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        return nil
    }
    return arguments[index + 1]
}

if arguments.contains("--check-permission") {
    if let url = URL(string: "ev://notify-check") {
        _ = NSWorkspace.shared.open(url)
    }
    print("requested")
    exit(0)
}

guard
    let title = value(for: "--title"),
    let body = value(for: "--body")
else {
    FileHandle.standardError.write(
        Data(
            """
            usage: EVNotificationHelper --id <id> --bundle-id <id> --title <t> --body <b>
                   EVNotificationHelper --check-permission
            """.utf8
        )
    )
    exit(2)
}

var components = URLComponents(string: "ev://notification")!
components.queryItems = [
    URLQueryItem(name: "id", value: value(for: "--id") ?? UUID().uuidString),
    URLQueryItem(name: "title", value: title),
    URLQueryItem(name: "body", value: body),
]

guard let url = components.url else {
    FileHandle.standardError.write(Data("EVNotificationHelper: could not build URL\n".utf8))
    exit(3)
}

if NSWorkspace.shared.open(url) {
    exit(0)
} else {
    FileHandle.standardError.write(
        Data("EVNotificationHelper: could not open EV.app (is it installed/running?)\n".utf8)
    )
    exit(1)
}
