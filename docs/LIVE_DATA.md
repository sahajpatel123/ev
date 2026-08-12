# Live Data & Sensors

EVIE's live-data layer records continuous, permissioned observations from
user-controlled collectors.  Every observation is an immutable
`live_events` row on a named `live_channels` channel with a fail-closed
privacy level, provenance (channel + collector + device), and a content hash
so the whole stream can be replayed and rebuilt.

## Privacy model

Collectors never emit raw screen pixels, raw audio, or exact coordinates.
They emit **derived, minimal representations**, and the server escalates any
event at least as restrictive as its channel:

| Channel | Kind | Default privacy | What is actually transmitted |
| --- | --- | --- | --- |
| `screen-activity` | `screen` | `sensitive` | Active app name, window/document title, code file, app category, browser URL, idle seconds, focus-session duration, meeting hint — text only |
| `audio-ambient` | `audio` | `sensitive` | Derived scene labels (speech/meeting/music/noise/silence), in-call flag, confidence; no raw audio unless the user explicitly consents to a raw-audio pipeline |
| `location-coarse` | `location` | `private` | Coarse place label + presence, e.g. "home"/"work"/"elsewhere" — never GPS coordinates |

`sensitive` events are stored for the user but excluded from the
model-facing slice unless explicitly opted in.  `private` coarse location
labels may reach the model slice.  `never_send_to_model` is the hard cap for
anything the user marks off-limits.

## Collector setup (macOS / Linux)

The collector agent lives in `backend/clients/collectors` and posts through
the same authenticated endpoints the CLI uses.

```bash
cd backend
export EV_API_URL="http://127.0.0.1:8000"
export EV_API_KEY="<master key or device token>"
export EV_DEVICE_ID="mac-collector"

# One sample, then exit
uv run python -m clients.collectors --once

# Or run continuously (default every 30 s)
uv run python -m clients.collectors --interval 30
```

Environment knobs:

| Variable | Meaning |
| --- | --- |
| `EV_API_URL` | Backend base URL (default `http://127.0.0.1:8000`) |
| `EV_API_KEY` | Bearer token (master key or registered device token) |
| `EV_DEVICE_ID` | Optional device id stamped on every event |
| `EV_SCREEN_CHANNEL_ID` / `EV_AUDIO_CHANNEL_ID` / `EV_LOCATION_CHANNEL_ID` | Optional pre-created channel ids; when set, events post to `/v1/live/channels/{id}/events`, otherwise the batch endpoint creates/uses the named channel |
| `EV_SCREEN_PRIVACY` / `EV_AUDIO_PRIVACY` / `EV_LOCATION_PRIVACY` | Optional per-channel privacy overrides (defaults above; `EV_LIVE_PRIVACY` is the legacy global override) |
| `EV_SCREEN_NATIVE` | `1` to enable the compiled Swift screen probe (NSWorkspace/CGWindow/AX) instead of AppleScript |
| `EV_SCREEN_OCR` | `1` to enable front-window OCR **only together with** `EV_SCREEN_CAPTURE_CMD` (explicit per-capture consent; derived text only) |
| `EV_SCREEN_CAPTURE_CMD` | User-configured shell command that emits front-window image bytes for OCR; never set by EV itself |
| `EV_LOCATION_NATIVE` | `1` to enable one-shot CoreLocation probing through the Swift helper |
| `EV_AUDIO_SAMPLE_FILE` | Optional WAV path classified by Agent 3's `app/audio/scene` interface |
| `EV_AUDIO_SCENE` / `EV_IN_CALL` / `EV_AUDIO_CONFIDENCE` | Derived audio hints from the OS or a local classifier |
| `EV_AUDIO_SCENE_FILE` | JSON hint file (`~/.ev/audio-scene.json`) with `{"scene": ..., "in_call": ..., "confidence": ...}` |
| `EV_LOCATION_PLACE` / `EV_LOCATION_PRESENCE` | Coarse place + presence from the OS layer |
| `EV_LOCATION_FILE` | JSON location file (`~/.ev/location.json`) with `{"place": ..., "presence": ...}` |
| `EV_LOCATION_PLACES_FILE` | JSON named places (`~/.ev/location-places.json`): `{"home": {"latitude": ..., "longitude": ..., "radius_m": 300}, "work": {...}}` |
| `EV_COLLECTOR_QUEUE_DIR` | Offline queue directory (default `~/.ev/collector_queue`) |
| `EV_COLLECTOR_QUEUE_MAX_RECORDS` | Queue record cap (default 10 000; oldest dropped first) |
| `EV_COLLECTOR_QUEUE_MAX_BYTES` | Queue byte cap (default 8 MiB; oldest dropped first) |
| `EV_COLLECTOR_PID_FILE` | Optional path where the collector writes its own PID on start (for resource probes / supervision) |
| `EV_COLLECTOR_HELPER_DIR` / `EV_COLLECTOR_HELPER_BIN` | Swift helper cache dir / prebuilt binary path |

## Native probes (Swift helper)

`backend/clients/collectors/swift/ambient_helper.swift` is compiled once into
`~/.ev/collector-helper/ambient_helper` and invoked as a short-lived process:

- `--screen` — frontmost app name, bundle id, app category, window title
  (CGWindow/AX), browser URL, idle seconds, and Accessibility / Screen
  Recording permission booleans.  No pixels are captured or emitted.
- `--location --no-prompt` — one-shot CoreLocation status + coarse presence,
  classified against `~/.ev/location-places.json`.  Never prints coordinates.
- `--location --prompt` — same, but requests When-In-Use authorization once
  (run this manually the first time).
- `--monitor --seconds N` — significant-location-change monitor; prints each
  update and keeps `~/.ev/location.json` fresh so the collector's file path
  stays live.

`EV_SCREEN_NATIVE` and `EV_LOCATION_NATIVE` opt into the native paths; on a
non-Darwin host, or when Swift/permissions are unavailable, both collectors
degrade to their text/env paths or emit nothing — they never substitute raw
captures as a fallback.

## OCR (Agent 6 seam)

The collector never implements its own pixel path.  When the user sets both
`EV_SCREEN_OCR=1` (explicit consent) and `EV_SCREEN_CAPTURE_CMD` (their own
capture hook), the captured bytes are passed to Agent 6's local vision
provider and only the derived OCR text is added to the screen payload.
Without either variable, no capture happens and no raw pixels are persisted.

## Offline queue, idempotency and backoff

Delivery is offline-first:

- Every batch carries an idempotency key and the full delivery envelope
  (channel, kind, privacy level, events) and is written to a bounded JSONL
  queue (`~/.ev/collector_queue/pending.jsonl`) when the API is unreachable
  or returns 5xx.
- The queue is bounded by record count (10 000) and bytes (8 MiB); the
  oldest records are dropped first, so an arbitrarily long outage cannot grow
  memory or disk without limit.
- Replay is byte-identical, and the server's unique `(channel_id, sha256)`
  content hash turns re-sends into no-ops, so a 1 h (or longer) outage
  replays without duplicates.
- Retries use exponential backoff (`interval × 2^failures`, capped at 10
  minutes); 4xx records are quarantined (dropped) because retrying them
  cannot succeed.

## Ingestion & streaming endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/live/events` | Batch ingestion: `{channel, kind, privacy_level, events[]}`; creates the channel if needed |
| `POST /v1/live/channels/{id}/events` | Append events to an existing channel (event privacy escalates to channel privacy) |
| `GET /v1/live/stream?access=user\|model&since=...` | SSE tail with replay-on-connect; model slice only gets permissioned derived context |
| `GET /v1/live/status` | Per-channel counts and 24 h totals |
| `POST /v1/live/rebuild` | Deterministically drop + replay the derived layer from immutable events |
| `POST /v1/live/retention?days=...&dry_run=true` | Plan/apply the retention window (dry-run by default) |

The stream is verified end-to-end in `tests/test_live_stream.py`: a collector
posts through the API and a subscriber receives the event over the stream
generator/SSE, with `access=model` receiving only the derived context slice.

## Retention and rebuild

Retention windows are **per channel**, and sensitive channels are deliberately
short-lived:

| Channel kind | Default retention | Override |
| --- | --- | --- |
| `screen`, `audio` (sensitive) | 30 days | `metadata.retention_days` on the channel |
| `location` (private coarse) | 90 days | `metadata.retention_days` on the channel |
| other kinds | `EV_LIVE_EVENT_RETENTION_DAYS` (default 90) | `metadata.retention_days` / API `days` |

`POST /v1/live/retention?days=N&dry_run=false` forces one window for every
channel.  Retention is conservative: only consumed events past the window are
eligible, the latest event of every channel is always kept, and
provenance-linked events (recognition logs, routine triggers) are protected.
Derived rollups are recomputed from the retained stream so they stay
deterministic and rebuildable.

`workers/scheduler.py` runs both jobs inside the existing 24/7 tick loop:

| Job | Default cadence | Config |
| --- | --- | --- |
| Retention | once a day | `EV_LIVE_RETENTION_INTERVAL_SECONDS` (default `86400`) |
| Derived-state rebuild | hourly | `EV_LIVE_REBUILD_INTERVAL_SECONDS` (default `3600`) |

Both jobs have CLI entrypoints in `workers/jobs.py` (`run_live_retention`,
`run_live_rebuild`) and are covered by `tests/test_live_retention.py` and
`tests/test_live_rebuild.py`.

## iOS collector hooks

The `EVClient` Swift package ships a client-side data model + upload path for
app-activity / screen-time style observations (`LiveCollector.swift`):

- `LiveActivitySample` — derived app/document/category/duration, no pixels;
- `LiveEventCreate` / `LiveBatchRequest` / `LiveEventOut` / `LiveChannelOut`
  — wire contract matching the backend schemas;
- `LiveActivityCollecting` — the hook an app implements with DeviceActivity /
  screen-time APIs (the app owns permission prompts and scheduling);
- `EVAPIClient.postLiveBatch(_:)` and
  `EVAPIClient.postLiveEvents(_:toChannel:)` — upload paths for both live
  ingestion endpoints;
- `LiveCollector` — turns samples into `app-activity` (kind `app`,
  privacy `sensitive`) events and uploads them.

No UI is included; Domain 13 owns the app surface.  `swift run EVClientCheck`
verifies the upload path with a mock URL protocol.

## Permissions a human must grant

| Signal | macOS permission | Needed for | How to turn it off |
| --- | --- | --- | --- |
| Screen app name + window title | Accessibility (System Settings → Privacy & Security → Accessibility) | NSWorkspace/AX and the System Events fallback | Revoke Accessibility; or unset `EV_SCREEN_NATIVE` and rely on the AppleScript fallback; screen events stop |
| Screen window title/URL via CGWindow | Screen Recording | window titles and browser URLs when the native probe is enabled | Revoke Screen Recording in System Settings; or unset `EV_SCREEN_NATIVE` |
| Front-window OCR | Screen Recording + explicit consent | OCR summary | Unset `EV_SCREEN_OCR` and `EV_SCREEN_CAPTURE_CMD`; no pixels are ever captured by default |
| Location | Location (System Settings → Privacy & Security → Location Services) | CoreLocation presence via the Swift helper | Revoke Location; or unset `EV_LOCATION_NATIVE`; the collector then reads only explicit hints/files |
| Microphone | Microphone (macOS/iOS) | On-device VAD/audio-scene pipeline only (Agent 3's ears) | Revoke Microphone; the shipped collector relays derived hints only and never records raw audio |
| iOS Screen Time / DeviceActivity | Settings → Screen Time | `LiveActivityCollecting` samples | Revoke Screen Time usage in Settings |
| Notifications | Notifications (optional) | Future live-signal alerts | Not needed for ingestion |

Denied/restricted Location TCC is surfaced once per collector process as a
`permission` field on the location channel, then suppressed until the status
changes, so the missing permission is visible without spamming the stream.

## Resource discipline

The collector is designed for 24/7 operation on an 8 GB machine.  The bounded
queue caps disk growth, the Swift helper is a short-lived process, and
`backend/clients/collectors/resource_probe.py` records a 24 h RSS/CPU/queue
curve:

```bash
cd backend
uv run python -m clients.collectors.resource_probe \
  --pid <collector-pid> --duration 86400 --interval 60 \
  --queue ~/.ev/collector_queue --out ~/.ev/collector-resource.jsonl
```

For a launchd-managed collector, point the probe at a PID file instead
(`--pid-file <path>`), which it re-reads every sample so restarts are tracked
automatically.

Acceptance targets: ≤ 40 MB RSS, ≤ 2% average CPU, 0 raw pixels persisted.
