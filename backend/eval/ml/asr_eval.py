"""ASR quality harness: LibriSpeech test-clean subset WER + latency.

This is Agent 4's voice eval. It drives the *configured* transcriber
(``get_transcriber()``), so it works against the hosted OpenAI-compatible
endpoint (API-first) or the local Parakeet engine once weights are registered.

The data root must contain the extracted OpenSLR test-clean tree::

    <root>/LibriSpeech/test-clean/<speaker>/<chapter>/*.flac + *.trans.txt

Download (public dataset, CC BY 4.0)::

    curl -L -o test-clean.tar.gz https://www.openslr.org/resources/12/test-clean.tar.gz
    tar xzf test-clean.tar.gz -C <root>

Agent 2 (Foundry) should wire the console alias so the ordered command works::

    [project.scripts]
    ev-eval = "eval.retrieval.cli:main"   # extend to dispatch "asr" -> eval.ml.asr_eval

Until then:

    cd backend && uv run python -m eval.ml.asr_eval --samples 10

Writes ``backend/eval/ml/asr_quality.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parent / "asr_quality.json"
DEFAULT_DATA_ROOT = Path.home() / ".ev" / "datasets" / "librispeech"


def _audio_duration_ms(path: str) -> float | None:
    """Best-effort audio duration in ms (PyAV, then ffprobe)."""

    try:
        import av

        with av.open(path) as container:
            duration = float(container.duration or 0) / av.time_base
        return duration * 1000 if duration > 0 else None
    except Exception:  # noqa: BLE001 - fall back to ffprobe
        pass
    try:
        import subprocess

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            seconds = float(probe.stdout.strip())
            return seconds * 1000 if seconds > 0 else None
    except Exception:  # noqa: BLE001 - duration is optional
        return None
    return None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Token-level WER via Levenshtein edit distance."""

    ref = _normalize(reference).split()
    hyp = _normalize(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for ref_index, ref_token in enumerate(ref, start=1):
        current = [ref_index]
        for hyp_index, hyp_token in enumerate(hyp, start=1):
            current.append(
                min(
                    previous[hyp_index] + 1,  # deletion
                    current[hyp_index - 1] + 1,  # insertion
                    previous[hyp_index - 1] + (ref_token != hyp_token),  # substitution
                )
            )
        previous = current
    return previous[-1] / len(ref)


def _load_samples(limit: int | None, data_root: Path) -> list[dict[str, str]]:
    test_clean = data_root / "LibriSpeech" / "test-clean"
    if not test_clean.is_dir():
        raise FileNotFoundError(
            f"LibriSpeech test-clean not found under {data_root}; "
            "download and extract https://www.openslr.org/resources/12/test-clean.tar.gz"
        )
    per_file: list[list[dict[str, str]]] = []
    for transcript_file in sorted(test_clean.rglob("*.trans.txt")):
        file_samples: list[dict[str, str]] = []
        for line in transcript_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            audio_id, reference = line.split(" ", 1)
            audio_path = transcript_file.parent / f"{audio_id}.flac"
            if audio_path.is_file():
                file_samples.append(
                    {"path": str(audio_path), "reference": reference.strip()}
                )
        if file_samples:
            per_file.append(file_samples)
    if not per_file:
        raise FileNotFoundError(f"no flac+transcript pairs found under {test_clean}")
    if limit is None:
        return [sample for file_samples in per_file for sample in file_samples]
    # Round-robin across chapters so a small subset is speaker-balanced
    # instead of taking the first N sequential clips from one chapter.
    samples: list[dict[str, str]] = []
    index = 0
    while len(samples) < limit:
        advanced = False
        for file_samples in per_file:
            if index < len(file_samples):
                samples.append(file_samples[index])
                advanced = True
            if len(samples) >= limit:
                break
        if not advanced:
            break
        index += 1
    return samples


async def _measure(samples: list[dict[str, str]]) -> dict:
    import base64

    from app.voice.asr import get_transcriber
    from app.voice.contracts import Transcript, TranscriptPartial

    transcriber = get_transcriber()
    results: list[dict] = []
    total_wer = 0.0
    final_latencies: list[float] = []
    first_partial_latencies: list[float | None] = []
    latency_ratios: list[float] = []
    saw_partial = False
    started = time.perf_counter()
    for index, sample in enumerate(samples, start=1):
        audio = Path(sample["path"]).read_bytes()
        encoded = base64.b64encode(audio).decode("ascii")
        sample_started = time.perf_counter()
        first_partial_ms: float | None = None
        stream = getattr(transcriber, "stream", None)
        final: Transcript | None = None
        if stream is not None:
            async for item in stream(audio_b64=encoded):
                is_partial = isinstance(item, TranscriptPartial)
                if is_partial:
                    saw_partial = True
                if is_partial and first_partial_ms is None:
                    first_partial_ms = (time.perf_counter() - sample_started) * 1000
                if isinstance(item, Transcript):
                    final = item
        if final is None:
            final = await transcriber.transcribe(audio_b64=encoded)
        hypothesis = final.text if final is not None else ""
        final_ms = (time.perf_counter() - sample_started) * 1000
        wer = word_error_rate(sample["reference"], hypothesis)
        total_wer += wer
        final_latencies.append(final_ms)
        first_partial_latencies.append(first_partial_ms)
        duration_ms = _audio_duration_ms(sample["path"])
        if duration_ms:
            ratio = final_ms / duration_ms
            latency_ratios.append(ratio)
        else:
            ratio = None
        results.append(
            {
                "index": index,
                "path": sample["path"],
                "reference": sample["reference"],
                "hypothesis": hypothesis,
                "wer": round(wer, 4),
                "confidence": final.confidence if final is not None else 0.0,
                "degraded": final.degraded if final is not None else True,
                "audio_duration_ms": (
                    round(duration_ms, 1) if duration_ms is not None else None
                ),
                "latency_ms": round(final_ms, 1),
                "latency_ratio_to_audio": round(ratio, 2) if ratio is not None else None,
                "first_partial_ms": (
                    round(first_partial_ms, 1)
                    if saw_partial and first_partial_ms is not None
                    else None
                ),
            }
        )
    elapsed = time.perf_counter() - started
    degraded_count = sum(1 for result in results if result["degraded"])
    payload = {
        "dataset": "LibriSpeech test-clean (OpenSLR 12)",
        "provider": transcriber.name,
        "streaming": saw_partial,
        "samples": len(results),
        "measured": degraded_count == 0,
        "degraded": degraded_count > 0,
        "wer_mean": round(total_wer / len(results), 4) if results else None,
        "wer_samples": results,
        "latency_ms_mean_final": (
            round(sum(final_latencies) / len(final_latencies), 1)
            if final_latencies
            else None
        ),
        "latency_ratio_mean_final": (
            round(sum(latency_ratios) / len(latency_ratios), 2)
            if latency_ratios
            else None
        ),
        "first_partial_ms_mean": (
            round(
                sum(v for v in first_partial_latencies if v is not None)
                / len([v for v in first_partial_latencies if v is not None]),
                1,
            )
            if any(v is not None for v in first_partial_latencies)
            else None
        ),
        "total_elapsed_s": round(elapsed, 2),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if degraded_count:
        payload["error"] = (
            f"{degraded_count}/{len(results)} transcripts degraded "
            "(provider unavailable); WER is not a real engine measurement"
        )
    return payload


async def _run(limit: int | None, data_root: Path) -> dict:
    samples = _load_samples(limit, data_root)
    try:
        return await _measure(samples)
    except Exception as exc:  # noqa: BLE001 - the JSON must record failures honestly
        return {
            "dataset": "LibriSpeech test-clean (OpenSLR 12)",
            "samples": len(samples),
            "measured": False,
            "error": f"{type(exc).__name__}: {exc}",
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ev-eval asr", description=__doc__)
    parser.add_argument("--samples", type=int, default=None, help="limit sample count")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help="directory containing LibriSpeech/test-clean (default ~/.ev/datasets/librispeech)",
    )
    args = parser.parse_args(argv)
    payload = asyncio.run(_run(args.samples, args.data_root))
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    if payload.get("measured") is False or payload.get("wer_mean") is None:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
