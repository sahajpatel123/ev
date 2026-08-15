import Darwin
import SwiftUI

// SwiftPM executable targets do not need a main.swift, but keeping one makes
// the @main App explicit and avoids any `-parse-as-library` ambiguity across
// toolchain versions.
let arguments = CommandLine.arguments
if arguments.contains("--smoke-test") {
    exit(EVSmokeTest.run())
} else if arguments.contains("--permissions") {
    exit(EVSmokeTest.runPermissions())
} else if arguments.contains("--notify-test") {
    exit(EVSmokeTest.runNotify())
} else if arguments.contains("--mic-test") {
    exit(EVSmokeTest.runMic())
} else if arguments.contains("--tts-test") {
    exit(EVSmokeTest.runTTS())
} else if arguments.contains("--life-request") {
    exit(EVSmokeTest.runLifeRequest())
} else if arguments.contains("--request-all") {
    exit(EVSmokeTest.runRequestAll())
} else if arguments.contains("--request-pending") {
    exit(EVSmokeTest.runRequestPending())
} else {
    // Bind the shared app to EVApplication before SwiftUI touches AppKit,
    // so last-window / stray terminate hits TerminatePolicy even when the
    // MenuBarExtra adaptor has not wired AppDelegate yet.
    _ = EVApplication.shared
    EVApp.main()
}
