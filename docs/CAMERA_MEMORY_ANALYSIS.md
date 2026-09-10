# Camera → Memory: field analysis (read before building "Evie remembers what she sees")

**Status:** analysis artifact, authored at the owner's request on 2026-09-10.
No code, schema, config, or test was modified to produce it.
**Tree:** working tree at `/Users/sahajpatel/Code/ev`, branch `iphone-evie-cycles`,
HEAD `b1ece14` + uncommitted work. Everything below was measured on this tree
while writing it; commands are quoted so it can be re-measured.
**Scope:** the whole path from "a camera captures an image/video" to "Evie can
recall it later and answer questions about it" — on the MacBook and on both
iPhones — plus the constraints, the drift, the honest gaps and the decisions
that must be made before implementation.

This document exists because the field **has no single source of truth today**.
`docs/VISION.md` describes Agent 6's package (`app/vision/**`) and is accurate
about it, but nothing in `docs/` describes `app/memory/visual.py`,
`app/ev/look.py`, `app/ev/camera_runtime.py`, `app/cognitive/capabilities.py`,
the phone camera routing, or the PWA's still-only limitation. That gap is the
main source of future confusion; this file closes it.

---

## 0. TL;DR — eleven facts that must not be rediscovered

1. **Images work, end to end.** MacBook camera, both iPhones (Safari PWA, which
   is the shipped phone path), Mac screen, kept JPEGs, derived memory, and
   spoken recall all exist and are heavily test-locked.
2. **Video does not work, anywhere in the server.** A clip's bytes never reach
   Home Station. Today "video" means *poster frames the client extracted*,
   plus a **memory sentence that claims a clip was recorded**.
3. **`record_video` is not reachable from the current mind.** In
   `EV_COGNITIVE_MODE=muse_kernel` (set in both `.env` and `.env.api-first`)
   the kernel exposes exactly 23 capabilities; the only vision one is
   `look.capture` (still frame). `record_video`, `capture_photo` and
   `observe_camera` remain reachable only through the legacy Mini tool path.
4. **The shipped iPhone path cannot record video** and says so in its own UI:
   `"Capturing a still image · video clips are not supported here"`
   (`backend/clients/pwa/app.js:3207`). The Swift `CameraManager.recordClip`
   that *can* record lives in the optional native track, not in the PWA.
5. **The server's local perception engines have never run.** `detect-rtdetr-nano`,
   `scene-mobileclip-s0`, `face-yunet` are **not in the ModelArbiter registry**
   (`backend/app/ml/registry.py`) and not on disk, so
   `create_detector()/create_scene_encoder()/create_face_detector()` always
   return honest doubles. The `docs/VISION.md` DEP REQUEST to Agent 2 never landed.
6. **Real vision that *does* run today** is: Apple Vision OCR through the
   `evvision` helper (server side, `EV_VISION_PROVIDER=apple_vision`), Apple
   Vision on the client (`VNRecognizeTextRequest`, `VNClassifyImageRequest`,
   face/human rectangles, colour stats), and the hosted mind looking at the
   actual JPEG (Muse Spark, `supports_media=True`, `input_image` only — no video).
7. **Pixels from a camera capture are only persisted for two cases:** `keep`
   (owner says "memorise this") and `capture_photo` / PWA `capture_save`.
   Ordinary looks and observes live in process memory with a TTL and are then
   gone. (Separately, any client may upload bytes through `POST /v1/attachments`
   — chat images, voice notes, shared files — which is a different path.)
8. **Visual memory is text.** `camera.observation` events + `Memory` rows +
   text embeddings. The MobileCLIP image embedding is computed and **thrown
   away**; there is no image index, no clip index, no frame timeline.
9. **Measured production reality (live Postgres, 2026-09-10):** 50
   `camera.observation` events, **21 of them (42%) contain no visual evidence at
   all** (no labels, no OCR, no pixels); only 4 JPEGs have ever been stored;
   **0 video observations**. Screen content and even Evie's own conversational
   replies have been written into durable "visual" memory.
10. **One vision test is red at HEAD**:
    `tests/test_camera_vision.py::test_lighting_and_dark_excuse_helpers` expects
    the phrase "natural sentences" that a later rewrite removed from
    `camera_model_instructions`. Running the field's tests: **126 passed, 1 failed**.
11. **Ownership is undefined for most of this field.** `app/device_gateway/**`,
    `app/cognitive/**`, `app/everywhere/**`, `app/memory/visual.py`,
    `backend/clients/pwa/**`, `app/life/**`, `app/presence/**` post-date
    `docs/AGENT_FLEET.md` §2 and appear in no OWNS list. Parallel work must
    assign ownership explicitly *before* the first edit.

---

## 1. How to re-measure everything here

```sh
cd /Users/sahajpatel/Code/ev

# tree metrics / drift
python3 tools/baseline.py

# the field's own tests (measured: 126 passed, 1 failed — see §0.10 / P0.1)
cd backend && uv run pytest tests/test_camera_vision.py tests/test_visual_identity.py \
  tests/test_look.py tests/test_spark_look.py -q

# neighbouring suites (measured: 125 passed, 1 skipped)
uv run pytest tests/test_perception.py tests/test_vision_ocr.py tests/test_vision_detect.py \
  tests/test_vision_corpus.py tests/test_people_recognition.py tests/test_iphone_memory.py \
  tests/test_device_gateway.py tests/test_pure_pwa_no_native_shell.py -q

# live reality (Postgres is the production store on this machine)
psql -U $USER -d ev -h localhost -c "
select event_type, count(*) from events
where event_type like 'camera%' or event_type like 'perception%' or event_type like 'photo%'
   or event_type like 'life.photo%' group by 1 order by 2 desc;"

# which engines are real
psql -U $USER -d ev -h localhost -c "
select jsonb_array_length(coalesce(content->'labels','[]'::jsonb)) as labels,
       coalesce(content->>'ocr_text','') <> '' as ocr,
       (content->>'attachment_id') is not null as pixels, count(*)
from events where event_type='camera.observation' group by 1,2,3 order by 4 desc;"
```

---

## 2. The map — every file that participates

### 2.1 Capture surfaces (where pixels are born)

| Surface | File | What it can do | Ships? |
| --- | --- | --- | --- |
| iPhone (Safari PWA) | `backend/clients/pwa/app.js` (`captureCamera` :3202, `handleCameraRequest` :3539) | one still via `getUserMedia` + canvas → JPEG; **no video**; records the clip request as a still after a 2 s wait | **yes — the primary phone product** (`docs/IPHONE_PRODUCT.md`) |
| iPhone (native, optional) | `ios/EVClient/Sources/EVClient/CameraFrameCapture.swift` (`CameraManager`, `recordClip` :190, `posterJPEGs` :867, `analyze` :970) | still, observe, **real clip recording** (VideoWriter + MovieFile fallback), on-device Apple Vision analysis, save to app dir + Photos | optional later track |
| iPhone listener | `ios/EVClient/Sources/EVClient/LiveVoiceCoordinator.swift` (`fulfillRecord` :590) | fulfils `camera_request` events over the live socket; sends posters only (`media_kind: "video"`), never the `.mov` | optional later track |
| MacBook (native app) | `macos/Sources/EV/LiveConversation.swift` (`fulfillRecord` :544) + the same `CameraManager` | same as native iPhone, plus a visor overlay for the last poster | yes |
| MacBook (helper) | `helpers/evvision/Sources/evvision/main.swift` | `ocr`, `screen` (frontmost window + OCR), `camera --once` (single frame, explicit request) | yes, built at `helpers/evvision/.build/release/evvision` |
| Mac screen | `app/ev/computer_runtime.py` + `evvision screen` | `screen_look` — screen pixels, OCR | yes (computer tool; **writes no visual memory of its own** — see §4.3) |
| Mac disk (no capture) | `app/services/life_stream_daemon.py` (`sync_photos` :966) | indexes **filenames only** from the Photos SQLite DB → `photo.library.indexed` | yes |

### 2.2 Server ingest (pixels → derived facts)

| Path | Entry point | What happens to the bytes |
| --- | --- | --- |
| Live look/observe/record (Talk) | `app/ev/look.py` (`look_now` :1526, `observe_camera_now` :2000, `capture_photo_now` :2168, `record_video_now` :2304) | frame arrives over the live socket (`app/voice/live/session.py` `_handle_look_frame` :1592); JPEG is stashed in-process (`CameraObservation`) for model injection; **stored as an attachment only for `keep` / `capture_photo`** |
| Phone polling camera | `app/device_gateway/phone_look.py` (`ingest_phone_frame`) via `POST /v1/device-gateway/camera/result` (`app/device_gateway/api.py:2891`) | OCR (Apple Vision) + client labels → `persist_visual_observation`; bytes are **not** stored |
| Phone live camera | `POST /v1/device-gateway/live/look-frame` (`api.py:1031`) → `inject_look_frame` | same live path as Mac; keep may persist the JPEG |
| Chat attachment | `POST /v1/chat` with `attachment_id` (`app/api/core.py:1516`) + `POST /v1/vision/analyze` (`app/api/edith.py:618`) | `app/ev/vision.py::analyze_attachment` :393 — OCR + local (double) detect/scene/face + optional mind call; writes a `perception.analyze` live event + `RecognitionLog`; **no durable visual memory** |
| Generic upload | `POST /v1/attachments` (`app/api/core.py:2181`) | stores the blob and an event; **no analysis, no size cap** |
| Importers (filenames only) | `app/memory/life_archive/**` | `life.photo.index` rows for Takeout zips (measured: 9,817) |

### 2.3 Memory (derived, durable)

| Piece | Location | Notes |
| --- | --- | --- |
| Camera observation writer | `app/memory/visual.py::persist_visual_observation` :3008 | writes one `camera.observation` Event + `Memory` rows via `MemoryWriter`; never raises into the look |
| Observation text | `app/memory/visual.py::visual_observation_text` :1704 | one sentence: lead ("I looked" / "I took a photo" / "I recorded a video clip" / "I watched the camera") + scene + labels + colours + OCR + duration + saved path |
| Keep ("memorise this") | `app/memory/visual.py` (`wants_keep_visible` :586, `keep_sight_text` :1388, `extract_visual_identity` :1079, `adopt_recent_spoken_keep` :2959) | a `fact` memory with `kind="visual_keep"`, importance 0.96; **the only path that requires stored pixels** |
| Spoken-scene adoption | `remember_spoken_scene` :3469, called from `app/memory/turns.py:85` and `app/voice/live/session.py:3121` | binds the mind's spoken description to the newest keep that has pixels |
| Re-read a stored image | `_enrich_keep_from_attachment` :2448 → `app/ev/vision.py::analyze_attachment` | the existing seam for "answer later from a stored image" |
| Object placement | `app/memory/room.py` (`extract_placement`, `placement_fact_text`) | "last seen on the desk" style facts |
| Retrieval | `app/memory/visual.py::search_visual_observations` :1819 (lexical tokens + recency) | also reached from `app/memory/recall.py:1038`, `app/memory/select.py:238`, and `app/device_gateway/pipeline.py:694` (`route: VISUAL_RECALL`) |
| Tables | `events`, `memories` (with `embedding` text vector), `attachments`, `recognition_logs`, `camera_states`, `owner_cameras`, `live_channels`/`live_events` | `app/models.py` — shared, append-only |

### 2.4 Deciding, speaking, showing

| Piece | Location | Notes |
| --- | --- | --- |
| Kernel capability (mind) | `app/cognitive/capabilities.py:262` `look.capture` → `app/cognitive/executor.py:344` | the **only** vision capability in `muse_kernel` mode; `screen: true` routes to `screen_look` |
| Turn guidance | `app/cognitive/context.py:101` | "call look.capture first, then speak from that capture" |
| Legacy tool specs | `app/ev/tools.py:1456` `look`, :1510 `observe_camera`, :1553 `capture_photo`, :1585 `record_video`, :1437 `camera_replay`, `screen_look` | `record_video` params: duration 2–30 s, default 8 |
| Live injection | `app/voice/live/grok_voice.py::_deliver_camera_images` :5056 | pushes each stashed JPEG into the realtime session as an `input_image` |
| Limits | `app/ev/camera_runtime.py`: `MAX_JPEG_BYTES` 1.5 MB, observe ≤ 8 s / 5 frames, record 2–30 s, `RECORD_MAX_POSTERS` 4 | |
| Phone Look history | `GET /v1/device-gateway/looks` (`api.py:2410`) + PWA `refreshLooks` | **text only** — no thumbnails, no playback anywhere in the PWA |
| Camera routing | `app/everywhere/endpoint_profile.py::resolve_camera_target` :134 | ranks by declared hardware (16 Pro = rank 0, SE = 10, Mac = 50), skips sandbox/denied/offline-last |

---

## 3. End-to-end flows (what is actually persisted, per flow)

Legend: `E` = `events` row, `M` = `Memory` row, `A` = object-store attachment.

**F1 — MacBook "what am I holding?" (still, current path in `muse_kernel`)**
`look.capture` → `look` tool → live socket `camera_request` → native client
`CameraManager.captureFrame` → Apple Vision analysis on device → `look_frame`
with JPEG + labels/OCR/faces/colours → stashed in process → mind gets
`input_image` → speaks → `persist_visual_observation` writes **E + M(text)**;
pixels **not** stored; stash expires.

**F2 — observe (bounded multi-frame, 4 s default, ≤ 5 frames)**
Same, but frames are merged into one sentence (`_observe_spoken`) and one
`camera.observation` with `media_kind="observe"`. Per-frame detail
(`frames_summary`) is **not** copied into the memory payload — it lives only in
the tool result.

**F3 — `capture_photo` (still that is saved)**
Client saves to app media dir + Photos, returns `saved_path`; server **stores the
JPEG as an attachment** (`store_frame_attachment`) → **E + M + A**, `media_kind="photo"`.

**F4 — `record_video` (the broken one)**
Client records `.mov` locally, extracts ≤ 3 poster JPEGs, saves the clip to the
device, sends **posters only** with `saved_path` and `duration_ms`.
Server: `record_video_now` stashes each poster, merges labels, and writes
**E + M** with `media_kind="video"` and `duration_s`. The clip bytes are
**never uploaded**; a `saved_path` (e.g. an iOS sandbox path) is spoken and
written into memory as if it were retrievable. The result carries
`persist_raw=True`, which is asserted by tests
(`tests/test_camera_vision.py:645`) but has **no production consumer** — for
`capture_photo` the pixels are stored because an attachment is created, not
because of this flag; for `record_video` the flag does nothing at all.
On the PWA path the same flow produces a **single still after a 2 s wait**, and
the memory still says *"I recorded a video clip … Duration N seconds"*.

**F5 — iPhone Talk look (shipped)**
`camera_request` over WebRTC/live → PWA `handleCameraRequest` → still → posts
`/live/look-frame` (or replies over the socket) → same as F1 with
`provenance="phone_camera"` and `device_id` = that iPhone. Keep stores the JPEG
(measured: 4 keeps, all `provenance=phone_camera`).

**F6 — iPhone look when Talk is not live (polling)**
`POST /v1/device-gateway/camera/result` → `ingest_phone_frame` → Apple Vision
OCR + client labels → **E + M**, `media_kind` = the action name
(`look_once`/`observe`/`capture_photo`/`record_clip`). Note
`media_kind="record_clip"` falls through `visual_observation_text`'s kind table
and is spoken as **"I looked"**.

**F7 — photo shared into chat / `POST /v1/vision/analyze`**
`analyze_attachment`: OCR (local) + local detect/scene/face (doubles) + optional
mind call with the raw image when `allow_raw` and the provider may see pixels →
one `perception.analyze` live event + `RecognitionLog(source="model")`.
**No durable visual memory** until the human confirms a recognition
(`POST /v1/vision/recognitions/{id}/confirm`), which writes a
`recognition_memory_candidate`. Measured: **0 `perception.analyze` events have
ever been written in production.**

**F8 — keep ("memorise this")**
Owner phrase → `wants_keep_visible` → `_keep_live_look_result` → JPEG stored
(`look-keep.jpg`) → `persist_visual_observation` writes an `observation`
(importance 0.9) **and** a `fact` with `kind="visual_keep"` (importance 0.96),
plus a thin-identity re-read pass (`_enrich_keep_from_attachment`) that
re-analyses the stored JPEG later. This is the most heavily test-locked
behaviour in the field (`tests/test_visual_identity.py`, 3,627 lines).

**F9 — Mac Photos library** → filenames only (199 rows). **F10 — Takeout
archive** → filenames/albums only (9,817 rows). Neither stores pixels.

---

## 4. What memory really contains (and where it lies)

### 4.1 Event + memory shape

`camera.observation` event content keys:
`text, labels, colors, people, saved_path, media_kind, visual_facts, spoken,
request_id, attachment_id, ocr_text, keep_request, object, objects, surface,
placement, provenance, duration_s`.
Metadata: `{visual: true, visor: true}` (+ `pii_categories` when the boundary
tagged something). Memory payload `kind ∈ {visual, visual_keep,
object_placement}`; retrieval is lexical + text-embedding, scored by hard-coded
constants (`score 0.94` for keeps, `0.9` otherwise).

### 4.2 Measured production audit (live Postgres, 2026-09-10)

| Metric | Value |
| --- | ---: |
| `camera.observation` events | 50 |
| … with **no labels, no OCR** (no visual evidence at all) | **21 (42%)** |
| … with labels | 29 |
| … with OCR text | 8 |
| … referencing a stored JPEG | 7 (pointing at 4 attachments) |
| … `media_kind="video"` | **0** |
| … with `saved_path` | 0 |
| `visual_keep` memories | 20 |
| `object_placement` memories | 15 |
| `attachments` rows in the whole DB | 4 (all `look-keep.jpg`) |
| `perception.analyze` events | 0 |
| `photo.library.indexed` / `life.photo.index` (filenames only) | 199 / 9,817 |
| Distinct keep requests behind the 50 rows | 10 |

### 4.3 Three concrete quality failures visible in the data

1. **Unrelated conversation stored as a scene.** A row whose
   `keep_request` is a real "I am holding something in my hand. Can you see and
   memorize it?" has `spoken` = an unrelated conversational reply, empty
   `labels`, `ocr_text = null`, `attachment_id = null` — and the memory text is
   `"I looked. <that reply>."` Recall for that keep therefore returns
   conversation, not a scene.
2. **PII and unrelated context inside "visual" memory.** Rows contain things the
   mind said while looking at a *camera* frame but that came from elsewhere
   (contact details, mail subjects, a question about desktop files) — because
   the mind's whole spoken reply is adopted as the `spoken` scene line and then
   written under the lead `"I looked."`. The memory cannot tell "I saw this in
   the room" from "this happened to be in my context". `screen_look` itself
   writes **no** `camera.observation` row (it is a computer tool — verified: no
   `MemoryWriter` call anywhere in `app/ev/computer*.py`), so this is
   speech-adoption leakage, not a screen/camera writer collision.
3. **Duplicates and fragments per keep.** Three rows for one keep within 4 s,
   including a truncated fragment (`"A young person with."`). `_keep_is_thin`
   and supersede logic exist precisely because of this, and it still leaks.

Root cause (code-level): `persist_visual_observation` accepts a scene line from
`result["spoken"]` whenever it is "usable", and `spoken` on the live path is
the mind's own reply; nothing requires a **grounding signal** (pixels delivered,
labels, colours, or OCR) before the row is written. There is no
`grounded` discriminator on the row, so later recall cannot tell a real sighting
from a spoken sentence that happened to mention something.

---

## 5. What is real vs. what is a double

| Engine | State on this machine | Evidence |
| --- | --- | --- |
| Apple Vision OCR (server, `evvision`) | **real** | binary built 2026-09-03; `EV_VISION_PROVIDER=apple_vision` in `.env.api-first` |
| Apple Vision on device (OCR, classify, faces, humans, colours) | **real** | `CameraFrameCapture.analyze` :970 |
| Hosted mind seeing stills (Muse Spark, `input_image`) | **real** | `app/gateway/muse_spark.py:272` `supports_media=True`, media → `input_image` |
| Hosted mind seeing **video** | **does not exist** | media parts handle `image`, `audio`, `document`, `text` — no video |
| tesseract | installed (`/opt/homebrew/bin/tesseract`) | fallback OCR provider |
| `detect-rtdetr-nano` | **double (no weights)** | not in `app/ml/registry.py`; `_model_path` returns None (`app/vision/detect.py:225`) |
| `scene-mobileclip-s0` | **double (no weights)** | same; the embedding it would produce is discarded anyway |
| `face-yunet` | **double (no weights)** | same; `face-sface` *is* installed and real (37 MB) but consumes YuNet crops that never come |
| faster-whisper base.en (ASR) | **real, weights present** | `~/.cache/huggingface/.../faster-whisper-base` (141 MB); `EV_VOICE_ASR_PROVIDER=faster_whisper` |
| ffmpeg / afconvert | **real** | `/opt/homebrew/bin/ffmpeg` (used today for audio only) |
| onnxruntime / opencv / pillow | installed in `backend/.venv` | so the ONNX path would work the moment weights are registered |
| `evvision`-style video frame extraction | **does not exist** | no `AVAssetImageGenerator`/ffmpeg frame path anywhere in `backend/app` |

**Inference-topology note (FLEET_LAW §14):** stills may go to the hosted mind
under `remote_processing_allowed()`; video cannot go anywhere hosted, so any
real video understanding must be **local frame extraction + per-still calls**.

---

## 6. Constraints that bind this work

- **FLEET_LAW §6** — the API contract is additive-only (477 paths / 518 ops
  locked in `backend/eval/contract_v1.json`); never hand-edit it.
- **FLEET_LAW §3** — `app/config.py`, `app/models.py`, `app/schemas.py`,
  `app/api/{core,ev,edith,companion,tools}.py`, `Makefile`, `.env.example` are
  shared append-only, inside `# --- AGENT <N> <CODENAME> ---` blocks.
- **FLEET_LAW §5** — new tables/columns need a migration whose `down_revision`
  is the Alembic head at the time of starting (25 migrations today).
- **FLEET_LAW §7** — the offline suite must stay green: no keys, no weights on a
  required path; new engines degrade to a deterministic double with
  `degraded=true`, and tests that need weights `skipif`, never fail.
- **FLEET_LAW §8** — no fabricated confidence, no echoing input as a result, no
  client-supplied security claims. **The "I recorded a video clip" sentence on a
  still-only path is a §8 problem.**
- **FLEET_LAW §9 + `docs/MODEL_BUDGET.md`** — any new model registers with the
  arbiter (name, license, source_url, sha256, disk/resident/peak MB, tier) and
  fits the 2400 MB ceiling / 600 MB on-demand slot. No weights = no claim.
- **FLEET_LAW §10/§11** — owner-consented data only; no ambient raw media to any
  model; no stranger identification; single owner; no public ports.
- **`docs/FROZEN_CONTRACTS.md`** — the Mac live-voice surfaces are frozen
  (owner-verified 2026-08-25). Camera work must not disturb them.
- **AGENTS.md §3a** — production deploy authority is
  `scripts/deploy_production.sh` only; no `launchctl kickstart`, no live-DB
  migrations, no destructive endpoints from a development session.

---

## 7. Honest gaps, ranked

| # | Gap | Evidence | Impact if not fixed |
| --- | --- | --- | --- |
| G1 | **No video ingest.** Clip bytes never leave the device; no frame extraction, no audio transcription, no timeline, no storage | `record_video_now` (look.py:2304); no ffmpeg/AVFoundation frame path in `backend/app` | "Evie memorises video" cannot be built on top of this |
| G2 | **Video claims are ungrounded.** `_spoken_from_frame(purpose="record")` (:405) always says a clip was recorded, and `saved_path` is a device-local path the server cannot fetch | look.py:405-421; 0 rows with `saved_path` | Memory asserts something false; recall repeats it |
| G3 | **Visual memory admits unverified scenes** (42% of rows have no evidence; conversational/PII leakage into the scene line) | §4.2/§4.3 | Poisons every future recall feature, including video |
| G4 | **No image/clip index.** MobileCLIP embedding computed then discarded; no visual similarity, no "find the photo where…" | `app/vision/scene.py:38` `embedding` unused outside the module | Recall is textual; a wrong sentence makes the image unfindable |
| G5 | **Sub-image detail is dropped.** `frames_summary`, per-frame timestamps and per-frame OCR never reach memory | `persist_visual_observation` payload keys | "What changed at 0:04?" is unanswerable |
| G6 | **The phone cannot record video** (PWA) and the capability is still advertised to the mind via the legacy tool catalog | app.js:3207; `app/ev/tools.py:1585` | Owner asks to record → gets a still, then a false sentence |
| G7 | **`record_video` is unreachable in `muse_kernel` mode** — 23 capabilities, only `look.capture` | `app/cognitive/capabilities.py` | The owner's live config cannot even attempt video |
| G8 | **No eval gate for vision quality.** `eval_gates` has 18 gates; the vision ones are `face_recognition` (skipped) and an env check | `app/scripts/eval_gates.py`; `docs/VISION.md` acceptance table is manual | No regression signal for perception/memory quality |
| G9 | **42% ungrounded rows have no owner-visible marker** — nothing in the API tells the owner "this memory has no pixels" | §4.2 | Trust erosion; hard to audit |
| G10 | **Mac Photos / Takeout photos are filenames only**; no iPhone Photos access at all | `sync_photos` :966; `life_archive/parse.py` | "Memorise my photo library" is not a thing yet |
| G11 | **No media retention/erasure category for clips** (`compliance/policy.py` categories: voiceprint, faceprint, training, live_audio, access_log, event, integration_cache) | `app/compliance/policy.py:17-35` | Clips would be kept forever with no policy; erasure would miss them |
| G12 | **`/v1/attachments` has no size cap and no streaming** | `app/api/core.py:2181` | A clip upload is buffered fully in RAM (8 GB machine) |
| G13 | **Ownership undefined for most of the field** | §11 | Two agents editing `device_gateway`/`pwa` will collide |
| G14 | **One red test at HEAD** | §9 | "the suite is green" is currently false for this field |

---

## 8. Decisions the owner must make (with a recommended default)

| # | Decision | Options | Recommended default |
| --- | --- | --- | --- |
| D1 | What does "memorise a video" mean? | (a) summary sentence only (b) summary + keyframe timeline + transcript (c) full clip stored and replayable | **(b) now, (c) behind a flag** — a timeline is what makes temporal questions answerable; the clip file is optional |
| D2 | Where does frame extraction happen? | (a) client posters only (b) server ffmpeg (c) new `evvision frames` Swift command (AVFoundation) | **(c) primary + (b) fallback**: no new Python dependency, works on macOS with CLT, honest when absent; posters stay as a last resort |
| D3 | How does an iPhone record video? | (a) Safari `MediaRecorder` feature-detect (b) PWA "burst clip" = N timestamped stills (c) native track (EvieShell/EVApp) (d) share-sheet upload of a Camera-app clip | **(b) as the honest default, (a) if the feature exists, (d) as the "my own recording" import path** — never claim video when only stills exist |
| D4 | Clip retention | forever / 30 d / 7 d / derive-then-delete | **derive-then-delete for pixels (7–30 d), keep derived memory forever**, new `compliance` category + purge job |
| D5 | Which mind call per clip? | one call with K frames / one call per frame / local-only | **one call, ≤ 6 frames, cost-capped**, local OCR always first |
| D6 | Does a clip need audio transcription? | no / yes, local whisper / yes, hosted ASR | **yes, local `faster_whisper` base.en (already installed)** — speech in a clip is memory gold |
| D7 | Grounding policy for visual memory | write everything / write only grounded rows / write both with an explicit `grounded` flag | **write both, but tag `grounded: true|false`, never let an ungrounded row be spoken as a scene, and exclude ungrounded rows from visual recall answers** |
| D8 | MacBook as a camera target | never / only when explicitly asked / ranked after phones | **keep ranked after phones (rank 50) but always eligible for "this Mac"** — it is the only video-capable device today |
| D9 | Photo-library depth | filenames only (today) / opt-in pixel analysis with a budget / full library index | **opt-in, budgeted, background, newest-first** — biggest "memorise my life" win after video, but it needs a cost model |
| D10 | Replay a clip from the phone | no / link to the stored attachment / transcode for the web | **link to the stored attachment** (Safari can play `.mov`/`.mp4` from `GET /v1/attachments/{id}`); no transcoding |

---

## 9. Proposed work plan (do not start before D1–D10 are answered)

Each phase is independently shippable, keeps the suite green, and ends with a
measurement.

### Phase 0 — Correctness and honesty (small, immediate)
- **P0.1** Fix the one red test: decide whether `camera_model_instructions`
  should regain "natural sentences" or the test should assert the new phrase
  (`tests/test_camera_vision.py:168` vs `app/ev/camera_runtime.py:558-600`).
- **P0.2** Make `_spoken_from_frame(purpose="record")` (`app/ev/look.py:405`)
  evidence-based: a clip sentence only when the client sent a clip
  (`has_clip`/`duration_ms`/`media_kind`) and a device-local `saved_path` is
  labelled as such (never spoken as something Home Station can open).
- **P0.3** Require a grounding signal in `persist_visual_observation`
  (attachment, delivered JPEG, labels, colours, or OCR). Otherwise store the row
  as `grounded: false` with no scene sentence — or skip it.
- **P0.4** Discriminate camera vs screen in the payload (`source: camera|screen`)
  so present and future recall can say which it was.
- **P0.5** Add a vision gate to `eval_gates` that fails if any stored
  `camera.observation` claims a media kind it has no evidence for
  (`no-fabrication` gate). This is the regression net for G2/G3.
- Tests: extend `tests/test_camera_vision.py`, `tests/test_visual_identity.py`;
  new `tests/test_visual_memory_honesty.py`.

### Phase 1 — Advertise only what a device can do
- **P1.1** Per-device media capability evidence in `endpoint_profile`
  (`{"still": true, "burst": true, "video": false}`) written from real client
  reporting; expose it in the device gateway's capability manifest.
- **P1.2** New kernel capability for recording (e.g. `look.record`) that is only
  listed when the routed target declares `video` or `burst` — and says which one
  it will actually do.
- **P1.3** PWA: implement the burst clip (N stills over T seconds with
  per-frame timestamps) and/or `MediaRecorder` when the browser supports it;
  keep the honest UI copy; never label a burst as a video.
- **P1.4** Native clients: add `has_clip: true` + a clip upload path (Phase 2
  endpoint) so the Mac (and the optional native iPhone track) can send bytes.
- Tests: `tests/test_pure_pwa_no_native_shell.py`, PWA JS tests under
  `backend/clients/pwa/tests/`, `make iphone-parity-check`.

### Phase 2 — Server-side clip ingest (the core new machinery)
- **P2.1** `POST /v1/vision/clip` (new, additive): device-token bound, content
  allowlist (`video/mp4`, `video/quicktime`), size cap, streaming to object
  storage (fix G12), idempotency key, privacy level, duration, request id.
- **P2.2** `app/vision/clips.py`: extract ≤ 6 keyframes with timestamps
  (ffmpeg today; `evvision frames` as the no-dependency path) and a 16 kHz mono
  WAV; deterministic and unit-testable with a tiny fixture clip.
- **P2.3** Per-frame perception: local OCR (Apple Vision) + optional mind pass
  with the frames, behind the existing privacy/`remote_processing_allowed()`
  gate; cost-capped and non-blocking (queue mode via `app/workers/jobs.py`,
  outbox pattern like `app/memory/outbox.py`).
- **P2.4** Memory: `media_kind="clip"` + a `moments[]` timeline
  (`t_start, t_end, labels, ocr, colours, people, thumb_attachment_id`) + a
  summary sentence + transcript segments; keep the derived text forever.
- **P2.5** Retention/erasure: new `compliance` category for clip pixels
  (default derived-then-delete), a purge entry point in
  `run_compliance_retention`, and inclusion in `compliance/erasure.py`.
- **P2.6** Playback: `GET /v1/attachments/{id}` already serves bytes; surface it
  in the phone Look history as a "play clip" affordance (device-token gated).
- Tests: `tests/test_clip_ingest.py`, `tests/test_clip_timeline.py`,
  fixtures under `backend/tests/data/` (tiny, license-free, generated).
- Eval: `clip_ingest_determinism` + `clip_recall` gates.

### Phase 3 — Recall depth
- **P3.1** `grounded` flag end-to-end (write, API, recall); recall must prefer
  grounded rows and must say "no grounded record" rather than a wrong scene.
- **P3.2** Visual index: an on-device Apple `VNGenerateImageFeaturePrint` vector
  (no download, no model registration) **or** register `scene-mobileclip-s0`
  with the arbiter if a CLIP-based index is preferred. New additive column/table
  + migration; keeps the existing text embedding untouched.
- **P3.3** Temporal answers: "what was happening at 0:06", "what did I say in
  the clip" served from `moments[]` + transcript.
- **P3.4** Re-look from memory: generalise `_enrich_keep_from_attachment` to
  clip keyframes ("look again at that clip I showed you").
- Tests: extend `tests/test_visual_identity.py` (do not weaken it),
  new `tests/test_visual_temporal_recall.py`.

### Phase 4 — Perceive from the library (optional, bigger)
- **P4.1** iPhone Photos: share-sheet import (the extension exists in
  `ios/EVShareExtension/` for files/URLs) or a native track; decide per D9.
- **P4.2** Mac Photos: extend `sync_photos` beyond filenames with opt-in,
  budgeted pixel analysis; keep `docs/LIVE_DATA.md` honest.
- **P4.3** Takeout importers: optionally analyse media referenced by
  `life.photo.index` on request (never ambient).

### Cross-cutting deliverables
- **Docs:** this file becomes the field's entry point; add one owner-facing
  paragraph to `docs/IPHONE_PRODUCT.md` (phone camera = still/burst, honest) and
  to `docs/VISION.md` (G-notes), and record the new env vars in
  `docs/ENVIRONMENT.md` (append-only).
- **Ownership:** publish a short addendum that assigns
  `app/memory/visual.py`, `app/device_gateway/**`, `app/cognitive/**`,
  `backend/clients/pwa/**`, `app/everywhere/**` before any parallel work starts.
- **Physical acceptance:** extend `scripts/ios/physical-acceptance.sh` with a
  two-iPhone video step (16 Pro records, SE records or honestly reports burst,
  Mac records) and a recall-after-restart check.

---

## 10. Glossary — the terms that get confused

| Term | Means | Does **not** mean |
| --- | --- | --- |
| `look` / `look.capture` | one still frame from the phone/Mac camera, described, remembered as text | screen capture, or a stream |
| `screen_look` | Mac screen (frontmost window, OCR) | the camera |
| `observe_camera` | ≤ 8 s, ≤ 5 frames, merged into one sentence | a video, or a stored clip |
| `capture_photo` | a still saved on the device **and stored server-side** | a clip |
| `record_video` | a `.mov` recorded on the device; **only posters reach the server** | anything Home Station can reopen |
| `keep` | "memorise this" — the only path that requires stored JPEG pixels | a normal look |
| `camera_replay` | R3 tool for owner-added NVR/IP cameras | phone or Mac camera capture |
| `photo.library.indexed` | Mac Photos **filenames** | photo pixels or EXIF |
| `life.photo.index` | Takeout archive **file names/albums** | imported media |
| `perception.analyze` | one model/OCR pass over an uploaded attachment | durable memory |
| `attachment` | bytes in the object store under key `attachments/<uuid>.bin` (local root `storage/`) | a memory |
| `degraded: true` | the deterministic double ran | a measured quality number |

---

## 11. Ownership today (from `docs/AGENT_FLEET.md` §2) and the holes

| Path | Listed owner |
| --- | --- |
| `backend/app/vision/**`, `backend/app/ev/vision.py`, `helpers/evvision/**` | 6 EYES |
| `backend/app/people/**`, `backend/app/ev/people.py` | 7 ROSTER |
| `backend/app/memory/{extraction,entities,importance,patterns,writer}.py`, `backend/app/services/{processor,consolidation,recall,rebuild,importer,event_service}.py` | 9 MNEMO |
| `backend/app/embeddings.py`, `backend/app/memory/retrieval.py`, `backend/app/rerank.py` | 8 SYNAPSE |
| `backend/app/ev/**` *except* vision/people/tools/tool_select/actions/companionship/personality/interaction/conversation | 15 ORACLE → this is where `look.py`, `camera_runtime.py`, `spark_look.py` live |
| `backend/app/api/core.py`, `backend/app/config.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/app/api/{ev,edith,tools}.py` | shared, append-only |
| `backend/app/memory/visual.py` | **no owner listed** (post-fleet file) |
| `backend/app/device_gateway/**` (15,440 lines) | **no owner listed** |
| `backend/app/cognitive/**`, `app/everywhere/**`, `app/life/**`, `app/presence/**`, `app/digital/**` | **no owner listed** |
| `backend/clients/pwa/**` (the shipped phone product) | **no owner listed** (`clients/{cli,web}` belong to 17) |
| `ios/**`, `macos/**` | 18 SUIT |

Practical consequence: **any work in this field is a cross-boundary change by
definition.** Before starting, name one owner for the camera/memory field and
write DEPENDENCY NOTES for 6 (vision engines), 7 (faces), 8/9 (memory +
retrieval), 18 (clients) and 20 (gates/ops).

---

## 12. What this analysis did **not** verify (honest limits)

- No physical iPhone test was run (no iPhone in this session): the PWA camera,
  both phones' routing, and `record_clip` behaviour on device are **read from
  code**, not observed. The last physical contract in `docs/FROZEN_CONTRACTS.md`
  covers Mac + iPhone Talk, not the camera.
- The full test suite was not run; only this field's suites (126 + 125 tests).
  One failure is reported in §0.10 / P0.1 and it is pre-existing at HEAD.
- No Swift build was run (`swift build` for `helpers/evvision`, `macos/`,
  `ios/`); the helper binary on disk is dated 2026-09-03 and was assumed
  working because `evvision` is the configured provider.
- The full end-to-end latency budget for a future clip pipeline (extraction +
  OCR + mind + memory) is **estimated from adjacent measurements**
  (`docs/VISION.md`: 480–1423 ms per screen capture, median 550 ms), not measured.
- Whether Muse Spark accepts **multiple images in one call** is unverified; the
  provider code builds one `input_image` part per media item, which suggests it
  does, but no live call was made.
- The production API on :8000 is currently running an older provider profile
  (`chat=xai`, `live=openai-realtime` in `/v1/health`) than the tree's Spark
  wiring; the running process was left untouched (deployment law).
