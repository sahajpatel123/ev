# Swift helper consolidation (proposal — needs owners' agreement)

Agents 6 (Vision OCR), 12 (calendar/location), 13 (screen/live collectors),
and 14 (notifications) each need small native macOS helpers. Today each one
would build its own Swift binary with its own toolchain story.

Proposal: host them here as SwiftPM executable/library targets in one
`macos/Sources/Helpers/` tree:

- `EVVisionOCR` — Vision framework OCR (Agent 6)
- `EVScreenCapture` — ScreenCaptureKit capture helpers (Agent 13)
- `EVCalendarLocation` — EventKit + CoreLocation accessors (Agent 12)
- `EVNotifications` — the EVNotificationHelper already shipped here (Agent 14)

Rules if agreed:

- Each helper is a small, dependency-free SwiftPM target built with
  `swift build` (CLT only).
- Each exposes a CLI contract like `EVNotificationHelper` so backend processes
  can exec it without embedding Swift.
- Owners keep their backend ownership; SUIT only hosts the Swift toolchain.
- Nothing is moved out of `backend/**` without the owning agent's sign-off.

Status: **proposal only.** No helper from Agents 6/12/13 has been moved here;
EVNotificationHelper is hosted here already because Agent 18 owns `macos/**`.
