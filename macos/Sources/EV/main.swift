import Darwin
import SwiftUI

// SwiftPM executable targets do not need a main.swift, but keeping one makes
// the @main App explicit and avoids any `-parse-as-library` ambiguity across
// toolchain versions.
let arguments = CommandLine.arguments
if arguments.contains("--smoke-test") {
    exit(EVSmokeTest.run())
} else if arguments.contains("--mac-control-probe") {
    exit(MacControlProbe.run())
} else if arguments.contains("--play-media-probe") {
    exit(MacControlProbe.runPlayMedia())
} else if arguments.contains("--finder-play-probe") {
    exit(MacControlProbe.runFinderPlay())
} else if arguments.contains("--search-domain-probe") {
    exit(MacControlProbe.runSearchDomain())
} else if arguments.contains("--generic-intent-probe") {
    exit(MacControlProbe.runGenericIntent())
} else if arguments.contains("--files-probe") {
    exit(MacControlProbe.runFiles())
} else if arguments.contains("--live-e2e") {
    exit(MacControlLiveE2E.run())
} else if arguments.contains("--permissions") {
    exit(EVSmokeTest.runPermissions())
} else if arguments.contains("--notify-test") {
    exit(EVSmokeTest.runNotify())
} else if arguments.contains("--mic-test") {
    exit(EVSmokeTest.runMic())
} else if arguments.contains("--tts-test") {
    exit(EVSmokeTest.runTTS())
} else if arguments.contains("--tts-continuity") {
    exit(EVSmokeTest.runTTSContinuity())
} else if arguments.contains("--first-audio-test") {
    exit(EVSmokeTest.runFirstAudioSurvival())
} else if arguments.contains("--live-speak-test") {
    exit(EVSmokeTest.runLiveSpeakSurvival())
} else if arguments.contains("--listener-presence-test") {
    exit(EVSmokeTest.runListenerPresenceOverlap())
} else if arguments.contains("--vp-echo-probe") {
    let vpOn = arguments.contains("--vp-on")
    exit(EVSmokeTest.runVPEchoProbe(vpOn))
} else if arguments.contains("--barge-in-probe") {
    exit(EVSmokeTest.runBargeInProbe())
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
