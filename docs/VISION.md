# EYES — EV Vision & Perception (Agent 6)

## Why this exists

EV previously had no real sight: "OCR" was a UTF-8 decode, "labels" were
regex-scraped LLM prose, and no camera or screen pixel was ever captured.
This document and the code it describes give EV eyes on the host Mac while
keeping the fleet laws intact: local-first, permissioned, human-confirmed,
and honest when a real engine is unavailable.

## Architecture

```text
Attachment bytes / screen / camera
        |
        v
helpers/evvision  (Swift CLI, CLT-only build)
  - ocr     : Vision VNRecognizeTextRequest (images + PDFs)
  - screen  : ScreenCaptureKit, frontmost window only, downscale before OCR
  - camera  : AVFoundation single-frame grab on explicit request
        |
        v
backend/app/vision/
  - providers.py    : AppleVisionProvider (Darwin default), Tesseract fallback,
                      deterministic double for CI; typed errors
  - capture.py      : screen capture wrapper (privacy_level default sensitive)
  - camera.py       : single-frame camera wrapper (consented + logged)
  - detect.py       : nano RT-DETR-class ONNX detector (COCO-80)
  - scene.py        : MobileCLIP-S0-class scene labels + image embedding
  - face.py         : YuNet face DETECTION only (boxes/landmarks/crops)
  - image_utils.py  : EXIF/GPS stripping (pure Python) + Pillow decode/resize
  - eval.py         : COCO-style mAP-proxy harness (IoU=0.5, per-class AP)
  - corpus.py       : deterministic 50-image synthetic spot-check corpus
  - spotcheck.py    : CLI for the detector >= 0.35 mAP-proxy acceptance gate
        |
        v
backend/app/ev/vision.py
  - permission + privacy gating (MODEL_BLOCKED_LEVELS / RAW_BLOCKED_LEVELS)
  - local labels -> RecognitionLog as source="model" (pending confirmation)
  - raw media never leaves the device unless allow_raw + normal privacy
```

## Helper CLI contract

`evvision` speaks one JSON contract on stdout (see
`helpers/evvision/README.md` for the exact shapes):

- `evvision ocr <path>` — text, per-line confidence, normalized bounding boxes,
  page count. Images and PDFs supported.
- `evvision screen [--persist <path>]` — frontmost window capture + OCR.
  Never writes a file without `--persist`; exit 3 on denied Screen Recording.
- `evvision camera --once [--persist <path>]` — one frame only; exit 3 on
  denied camera permission; never writes without `--persist`.
- `evvision --selftest-ocr <dir>` — renders 10 synthetic PNGs, OCRs them, and
  reports character accuracy against ground truth (pass >= 0.95).
- `evvision --selftest-pdf <dir>` — renders 10 PDFs and reports the same
  character-accuracy metric for the PDF path.
- `evvision ocr|screen|camera` outputs include `elapsed_ms` and `peak_rss_mb`
  so latency/memory gates are measured per call.

## OCR providers

| Provider | Engine | Degraded when unavailable |
| --- | --- | --- |
| `apple_vision` | `evvision` helper (Vision framework) | Darwin default; falls back to `deterministic` only when the binary is absent |
| `tesseract` | tesseract binary, TSV parsing for lines/confidence | raises `VisionBinaryError` / `VisionEngineError` — never an empty success |
| `deterministic` | UTF-8 printable text extraction | always `degraded=True` (CI double, not a real OCR engine) |

`get_vision_provider()` resolves `EV_VISION_PROVIDER` explicitly; when unset on
Darwin and the helper exists, it selects `apple_vision` automatically (skipped
under pytest so the offline suite stays deterministic).

Vision-specific settings live in `app/vision/settings.py` (all `EV_`-prefixed):

| Env var | Default | Meaning |
| --- | --- | --- |
| `EV_VISION_EVVISION_BINARY` | `evvision` | helper binary name/path; repo-local `.build/release/evvision` is auto-found |
| `EV_VISION_EVVISION_AUTO` | `true` | auto-select Apple Vision on Darwin |
| `EV_VISION_DETECT_ENGINE` / `_MODEL` | `auto` / `detect-rtdetr-nano` | detector engine + registry model |
| `EV_VISION_SCENE_ENGINE` / `_MODEL` | `auto` / `scene-mobileclip-s0` | scene encoder engine + registry model |
| `EV_VISION_FACE_ENGINE` / `_MODEL` | `auto` / `face-yunet` | face detector engine + registry model |
| `EV_VISION_SCREEN_PRIVACY_LEVEL` | `sensitive` | screen capture default privacy level |
| `EV_VISION_SCREEN_MAX_DIMENSION` | `1280` | downscale cap before OCR |
| `EV_VISION_CAPTURE_TIMEOUT` | `30` | helper subprocess timeout (seconds) |

## Detector acceptance gate (mAP-proxy)

`app/vision/eval.py` computes the COCO-style mAP proxy (IoU 0.5 matching,
all-point AP interpolation, per-class TP/FP/FN/AP notes). The 50-image
spot-check corpus is generated locally by `app/vision/corpus.py`: 50
deterministic, license-free synthetic images with exact ground-truth boxes
in normalized coordinates (no downloads, no third-party photos). It is
reproducible (fixed seed; sha256 is stable for the same environment).

Run the gate once Agent 2 pins and downloads the detector weights:

```sh
cd backend
uv run python -m app.vision.spotcheck [CORPUS_DIR] [--engine auto|onnx|double]
```

Without a pinned model, the honest double runs and reports
`degraded: true` with mAP 0.0 and per-class FN counts — never a fabricated
score. The synthetic corpus measures detection matching/geometry; a
real-photo COCO spot check can be dropped into the same harness as an
additional gate when a license-checked dataset is available.

## Screen & camera privacy

- Screen capture is frontmost-window-only, downscaled to max dimension 1280
  before OCR, and pixels are discarded immediately.
- Default `privacy_level` is `sensitive`. Raw frames are never persisted unless
  the caller explicitly opts in per capture (`persist=True` + output path).
- Denied Screen Recording permission raises `ScreenRecordingDeniedError` with
  a clear message; denied camera permission raises
  `CameraPermissionDeniedError`.
- Camera capture is single-frame, explicit-request only, and logged with the
  consent reason. No background stream exists.

## Local perception engines

| Model | Task | Disk | Resident | Tier | License |
| --- | --- | ---: | ---: | ---: | --- |
| `detect-rtdetr-nano` | COCO object detection | ~12 MB | ~12 MB | on_demand | Apache-2.0 (RT-DETR, `github.com/lyuwenyu/RT-DETR`) |
| `scene-mobileclip-s0` | open-vocab scene labels + embedding | ~60 MB | ~60 MB | on_demand | Apple ML Research Model (weights, `github.com/apple/ml-mobileclip`) |
| `face-yunet` | face detection boxes/landmarks | 0.3 MB | 0.3 MB | on_demand | Apache-2.0 (OpenCV Zoo, `github.com/opencv/opencv_zoo`) |

YOLOv8/YOLO11 were rejected because Ultralytics models are AGPL-3.0. MobileCLIP
*code* is permissive, but the *weights* are under Apple's ML Research Model
license — recorded here and flagged for legal review before product use.
These entries are seed requests for Agent 2 (Foundry): source URLs and license
strings are in the DEP REQUEST; checksums must be pinned before download.

When a model or `onnxruntime` is absent, each factory returns the honest
deterministic double: `degraded=True` and **no** boxes, labels, embeddings, or
faces. No fabricated confidence values are ever produced.

## Human confirmation flow

Suggested labels (from OCR rules, the LLM, or local perception) land in
`RecognitionLog` with `source="model"` and `entity_id=None`. Nothing becomes a
durable recognition until the human confirms via
`POST /v1/vision/recognitions/{id}/confirm`, which promotes it to
`source="user"` and writes an observation memory with provenance. The system
never auto-asserts an inference as fact.

Face detection boxes and aligned crops are available through
`app.vision.face.aligned_crop` for Agent 7 (ROSTER) to consume; this module
never computes identity, embeddings of identity, or names, and boxes are not
persisted in perception payloads (only a count).

## never_send_to_model boundary

`MODEL_BLOCKED_LEVELS` includes `sensitive` and `never_send_to_model`; raw
media is further blocked for `private`. Tests prove that a
`never_send_to_model` attachment never reaches a provider: no chat call, no
media part, `raw_sent=False`, and a blocked summary. Local on-device analysis
is allowed for those sources because nothing leaves the device.

## EXIF / GPS

`strip_exif_gps()` removes JPEG APP1/APP2/APP13 segments and PNG `eXIf` chunks
with pure Python (works before Pillow is installed). Pillow (via Agent 2) adds
decode, resize, and EXIF-aware transpose. The storage integration point
(strip before write) is a dependency note for the storage owner.

## Acceptance status (measured)

| Gate | Status |
| --- | --- |
| `swift build -c release` (CLT only) | PASS — helpers/evvision |
| OCR character accuracy (10 synthetic PNGs, ground truth) | 99.27% average |
| PDF character accuracy (10 generated PDFs, ground truth) | 100% average (10/10) |
| OCR accuracy on 20 REAL frontmost-window screenshots vs ground truth | 99.80% average both runs; 20/20 >= 95% |
| Screen permission denial | PASS (exit 3, clear message) |
| Screen capture real (frontmost window + OCR) | PASS — 20 real captures: 480–1423 ms, 50.8–59.0 MB peak RSS; median 550–638 ms (borderline vs 600 ms; a resident helper removes per-call warm-up) |
| Camera single frame | PASS (FaceTime HD Camera, discarded; persist only with opt-in) |
| Detector >= 0.35 mAP-proxy on 50 images | NOT RUN on weights — corpus generator + harness + CLI exist and are tested; weights not yet pinned/downloaded (Foundry) |
| never_send_to_model provably never reaches a provider | PASS (tests) |

## Honest gaps

1. The first screen capture after idle pays AppKit/Vision warm-up (up to
   ~2.8 s); steady-state calls are ~550 ms median and always under 60 MB peak
   RSS in measurement. A resident helper process would remove the warm-up.
2. Real ONNX engines (RT-DETR-nano, MobileCLIP-S0, YuNet) are not downloadable
   until Agent 2 pins checksums; until then the suite exercises the double and
   stub sessions, not trained weights. The full 50-image mAP-proxy run is a
   single command once the weights exist (`app.vision.spotcheck`).
3. The synthetic spot-check corpus is abstract shapes, so it proves the
   harness, matching, per-class notes, and geometry — not how a real COCO
   detector generalizes to photographs. A license-checked real-photo COCO
   spot check remains a follow-up gate.
4. The 10 real screenshots used TextEdit windows with known sentences (owner
   could repeat with their own documents); all passed >= 95%.
5. MobileCLIP weights are under Apple's ML Research license — legal review
   needed before commercial use.

## Live `look` tool

Voice and HTTP can now call `look` (permission `vision:read`, risk R1). That
path is one consented frame, not a stream:

1. Live session emits `camera_request` action `capture`.
2. Mac/iOS grabs one JPEG, uploads `/v1/attachments`, replies `look_frame`.
3. Backend OCR (Apple Vision / tesseract / optional self-hosted DeepSeek-OCR)
   plus local detect/scene/face-count.
4. Enrolled owner objects and consented roster faces may be named. Strangers
   stay unnamed.
5. DeepSeek chat, when configured, rewrites the spoken sentence from derived
   text and labels only. Official `api.deepseek.com` cannot inspect pixels.

The capability appears on the live operator sheet as **camera look** and in
the runtime manifest as `look` (`provider=vision`). `camera_replay` remains
the separate R3 owner-NVR tool and stays off the realtime catalog.
