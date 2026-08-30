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

let presenting = arguments.contains("--present")

guard
    let title = value(for: "--title"),
    let body = value(for: "--body")
else {
    FileHandle.standardError.write(
        Data(
            """
            usage: EVNotificationHelper --id <id> --bundle-id <id> --title <t> --body <b>
                   EVNotificationHelper --present --title <t> --body <b> [--kind card] [--size card] [--time-type linger] [--place center] [--ttl 30000] [--id window] [--lookout]
                   EVNotificationHelper --check-permission
            """.utf8
        )
    )
    exit(2)
}

var components = URLComponents(string: presenting ? "ev://present" : "ev://notification")!
var queryItems = [
    URLQueryItem(name: "id", value: value(for: "--id") ?? UUID().uuidString),
    URLQueryItem(name: "title", value: title),
    URLQueryItem(name: "body", value: body),
    URLQueryItem(name: "kind", value: value(for: "--kind") ?? "card"),
]
if let size = value(for: "--size") { queryItems.append(URLQueryItem(name: "size", value: size)) }
if let time = value(for: "--time-type") { queryItems.append(URLQueryItem(name: "time", value: time)) }
if let place = value(for: "--place") { queryItems.append(URLQueryItem(name: "place", value: place)) }
if let ttl = value(for: "--ttl") { queryItems.append(URLQueryItem(name: "ttl", value: ttl)) }
if let items = value(for: "--items") { queryItems.append(URLQueryItem(name: "items", value: items)) }
if let questions = value(for: "--questions") { queryItems.append(URLQueryItem(name: "questions", value: questions)) }
if let recommendation = value(for: "--recommendation") {
    queryItems.append(URLQueryItem(name: "recommendation", value: recommendation))
}
if let source = value(for: "--source") { queryItems.append(URLQueryItem(name: "source", value: source)) }
if let response = value(for: "--response") { queryItems.append(URLQueryItem(name: "response", value: response)) }
if let layout = value(for: "--layout") { queryItems.append(URLQueryItem(name: "layout", value: layout)) }
if let dx = value(for: "--dx") { queryItems.append(URLQueryItem(name: "dx", value: dx)) }
if let dy = value(for: "--dy") { queryItems.append(URLQueryItem(name: "dy", value: dy)) }
if let tilt = value(for: "--tilt") { queryItems.append(URLQueryItem(name: "tilt", value: tilt)) }
if arguments.contains("--lookout") { queryItems.append(URLQueryItem(name: "lookout", value: "1")) }
components.queryItems = queryItems

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
