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
} else {
    EVApp.main()
}
