import Foundation
import UserNotifications

// PULSE macOS notification helper.
// Usage:
//   EVNotificationHelper --id <uuid> --bundle-id <id> --title <title> --body <body>
//   EVNotificationHelper --check-permission
//
// Sends via UNUserNotificationCenter so notifications appear in Notification
// Center with every app closed, support actions, and respect Focus modes.
// Exits non-zero with a human-readable reason when permission is denied or
// the request fails.

func value(after key: String, in args: [String]) -> String? {
    guard let index = args.firstIndex(of: key), args.indices.contains(index + 1) else {
        return nil
    }
    return args[index + 1]
}

let arguments = CommandLine.arguments

if arguments.contains("--check-permission") {
    let semaphore = DispatchSemaphore(value: 0)
    UNUserNotificationCenter.current().getNotificationSettings { settings in
        switch settings.authorizationStatus {
        case .authorized:
            print("authorized")
        case .denied:
            print("denied")
        case .notDetermined:
            print("notDetermined")
        case .provisional:
            print("provisional")
        case .ephemeral:
            print("ephemeral")
        @unknown default:
            print("unknown")
        }
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 10)
    exit(0)
}

guard let title = value(after: "--title", in: arguments),
      let body = value(after: "--body", in: arguments),
      let identifier = value(after: "--id", in: arguments) else {
    FileHandle.standardError.write(Data("missing --id/--title/--body\n".utf8))
    exit(2)
}

let center = UNUserNotificationCenter.current()
let semaphore = DispatchSemaphore(value: 0)
var permissionDenied = false

center.getNotificationSettings { settings in
    switch settings.authorizationStatus {
    case .authorized, .provisional, .ephemeral:
        permissionDenied = false
    case .denied:
        permissionDenied = true
        FileHandle.standardError.write(
            Data("notification permission denied; enable EV in System Settings > Notifications\n".utf8)
        )
    case .notDetermined:
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if let error = error {
                permissionDenied = true
                FileHandle.standardError.write(Data("permission error: \(error.localizedDescription)\n".utf8))
            } else if !granted {
                permissionDenied = true
                FileHandle.standardError.write(Data("notification permission denied\n".utf8))
            }
        }
    @unknown default:
        permissionDenied = true
    }
    semaphore.signal()
}
_ = semaphore.wait(timeout: .now() + 10)
if permissionDenied {
    exit(3)
}

let content = UNMutableNotificationContent()
content.title = title
content.body = body
content.sound = .default
content.threadIdentifier = "ev.pulse"

let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
center.add(request) { error in
    if let error = error {
        FileHandle.standardError.write(Data("delivery failed: \(error.localizedDescription)\n".utf8))
        exit(4)
    }
    exit(0)
}

// Keep the process alive until the add callback fires.
RunLoop.current.run()
