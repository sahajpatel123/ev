# iOS capability matrix (v1)

Vocabulary: SCAFFOLDED / IMPLEMENTED / INTEGRATED / VERIFIED / UNKNOWN.

Voice on native WKWebView: **UNKNOWN**. PWA golden: **OWNER VERIFIED CLEAN**.

AlarmKit: public on **iOS 26+**. Fallback on older OS: Evie local notification. Never claim Clock.

| Capability | Public API | Min iOS | Permission | Direct? | Sys UI? | Verify? | Primary | SE |
|---|---|---|---|---|---|---|---|---|
| Contacts | Contacts | 17 | Contacts (limited OK) | read | no | medium | SCAFFOLDED | SCAFFOLDED |
| Timer | AlarmKit or UNNotification | 26 / 17 | AlarmKit or notifications | yes | no | strong / medium | SCAFFOLDED | Evie notification fallback |
| Alarm | AlarmKit or UNNotification | 26 / 17 | same | yes | no | medium | SCAFFOLDED | fallback |
| Reminder | EventKit | 17 | Reminders | yes | no | strong | SCAFFOLDED | SCAFFOLDED |
| Calendar | EventKit write-only | 17 | Calendar | yes (no invites) | no | strong | SCAFFOLDED | SCAFFOLDED |
| Location | Core Location when-in-use | 17 | Location When In Use | read | no | medium | SCAFFOLDED | SCAFFOLDED |
| Notifications | UserNotifications | 17 | Notifications | yes | no | medium | SCAFFOLDED | SCAFFOLDED |
| Haptics | UIFeedbackGenerator | 17 | none | yes | no | strong | SCAFFOLDED | SCAFFOLDED |
| Phone | `tel:` handoff | 17 | Contacts if by name | no | yes | weak | SCAFFOLDED | SCAFFOLDED |
| Message | MessageUI | 17 | Contacts if by name | no | Apple Send | weak | SCAFFOLDED | SCAFFOLDED |
| FaceTime | `facetime:` | 17 | Contacts if by name | no | yes | weak | SCAFFOLDED | SCAFFOLDED |
| Maps | maps.apple.com | 17 | none | open | maybe | medium | SCAFFOLDED | SCAFFOLDED |
| App launch | curated universal links | 17 | none | open | no | medium | SCAFFOLDED | SCAFFOLDED |
| Share | UIActivityViewController | 17 | none | no | sheet | medium | SCAFFOLDED | SCAFFOLDED |
| Clipboard | UIPasteboard | 17 | none | yes | no | strong | SCAFFOLDED | SCAFFOLDED |

Kill switch: `EV_NATIVE_ACTIONS_ENABLED` (server). Disables actions without breaking voice.
