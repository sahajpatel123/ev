# evvision — Apple Vision helper for EV

`evvision` is a zero-dependency Swift Package Manager executable that gives EV
real vision on macOS using the system Vision framework, ScreenCaptureKit and
AVFoundation. It builds with Command Line Tools only (no Xcode, no external
packages):

```sh
cd helpers/evvision
swift build -c release
```

Binary: `.build/release/evvision`

## Commands

```text
evvision ocr <input-path> [--level fast|accurate]
evvision screen [--persist <path>] [--level fast|accurate]
evvision camera --once [--persist <path>]
evvision --selftest-ocr <tmpdir>
evvision --selftest-pdf <tmpdir>
evvision --version
```

## Output contract (stdout JSON)

### `ocr`

Exit 0:

```json
{
  "provider": "apple_vision",
  "text": "line one\nline two",
  "lines": [
    {
      "text": "line one",
      "confidence": 0.97,
      "bounding_box": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.1}
    }
  ],
  "page_count": 1
}
```

Exit 2: `{"error": {"code": "invalid_input|engine_error", "message": "..."}}`.
Bounding boxes are normalized 0...1 with a top-left origin.
Success JSON also reports `elapsed_ms` and `peak_rss_mb`.

### `screen`

Captures the frontmost window only, downscales to max dimension 1280 before
OCR, discards pixels, and **never writes a file unless `--persist <path>` is
given**. Exit 3 (`screen_recording_denied`) surfaces a denied Screen Recording
permission.

### `camera`

Single-frame grab on explicit request only — never a stream. Exit 3
(`camera_denied`) surfaces a denied camera permission; exit 2 (`no_camera`)
means no capture device. Writes a file only with `--persist <path>`.

## Notes

- Recognition level is `.accurate` with an automatic fallback to `.fast` when
  the system's accurate text engine fails (observed as
  `TextRecognition.CRImageReaderError` on some macOS 27/CLT builds). The
  fallback is cached in the temp directory so repeated invocations skip the
  broken level.
- `--level fast` is available for large, high-contrast text when latency
  matters more than small-text accuracy (default `.accurate`).
- `--selftest-ocr` renders 10 synthetic PNGs and reports character accuracy
  against ground truth; `--selftest-pdf` does the same for 10 generated PDFs
  (both pass at >= 0.95 average).
- `screen`/`camera` output also reports `elapsed_ms` and `peak_rss_mb` so the
  <= 600 ms / <= 60 MB capture gates are measurable per call.
- The binary embeds an Info.plist with `NSCameraUsageDescription` so the
  camera permission prompt is meaningful.

The backend-side detector acceptance gate (`>= 0.35 mAP-proxy on 50 images`)
uses `app/vision/corpus.py` (deterministic, license-free synthetic corpus)
and `app/vision/spotcheck.py` (CLI runner) — see `docs/VISION.md`.
