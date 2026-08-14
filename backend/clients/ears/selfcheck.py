"""Readiness self-check for the ears stack (no data required to run).

Prints what is installed, which models are configured, whether the microphone
is reachable (and permission is granted), and which human-collected datasets
are still missing for the acceptance gates.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def check_environment() -> dict:
    """Return a JSON-serializable readiness report."""

    from app.audio.models import model_arbiter
    from app.config import settings

    repo_root = Path(__file__).resolve().parents[3]
    wake_data_dir = Path(settings.ears_data_wake_dir or repo_root / "backend" / "data" / "wake")
    wake_head_path = (
        Path(settings.voice_wake_openwakeword_model_path).expanduser()
        if settings.voice_wake_openwakeword_model_path
        else None
    )
    wake_verifier_path = (
        Path(settings.voice_wake_openwakeword_verifier_path).expanduser()
        if settings.voice_wake_openwakeword_verifier_path
        else None
    )
    report: dict = {
        "dependencies": {
            "numpy": importlib.util.find_spec("numpy") is not None,
            "onnxruntime": importlib.util.find_spec("onnxruntime") is not None,
            "sounddevice": importlib.util.find_spec("sounddevice") is not None,
            "openwakeword": importlib.util.find_spec("openwakeword") is not None,
            "pyannote.audio": importlib.util.find_spec("pyannote") is not None,
        },
        "models": {
            "wake_head": bool(wake_head_path and wake_head_path.is_file()),
            "wake_head_configured": bool(wake_head_path),
            "wake_head_path": str(wake_head_path) if wake_head_path else None,
            "wake_verifier": bool(wake_verifier_path and wake_verifier_path.is_file()),
            "wake_verifier_configured": bool(wake_verifier_path),
            "vad": bool(settings.ears_vad_model_path),
            "scene": bool(settings.ears_scene_model_path),
        },
        "privacy": {
            "consent": settings.ears_consent,
            "api_url": bool(settings.ears_api_url),
            "save_segments_dir": bool(settings.ears_save_segments_dir),
        },
        "data": {
            "wake_clips": any(
                (wake_data_dir / "clips").glob("evie-*.wav")
            ),
            "ambient": any(
                (wake_data_dir / "ambient").glob("ambient-*.wav")
            ),
            "vad_labels": bool(settings.ears_data_vad_labels),
            "scene_labels": bool(settings.ears_data_scene_labels),
            "capture_wizard_dir": str(wake_data_dir),
        },
        "eval": {
            "wake_reliability_artifact": str(
                Path(__file__).resolve().parents[3] / "eval" / "ml" / "wake_reliability.json"
            ),
            "wake_reliability_measured": (
                Path(__file__).resolve().parents[3] / "eval" / "ml" / "wake_reliability.json"
            ).is_file(),
        },
        "microphone": {"reachable": False, "permission_denied": False, "devices": []},
        "arbiter": {},
    }
    try:
        model_arbiter().pin_always()  # reserve the locked always-tier roster
        stats = model_arbiter().stats()
        report["arbiter"] = {
            "resident_total_mb": stats["resident_total_mb"],
            "resident_by_tier_mb": stats["resident_by_tier_mb"],
            "ceiling_mb": stats["ceiling_mb"],
        }
    except Exception as exc:
        report["arbiter"] = {"error": str(exc)}

    if report["dependencies"]["sounddevice"]:
        try:
            from app.audio.capture import MicrophoneDeniedError, list_input_devices

            report["microphone"]["devices"] = list_input_devices()
            report["microphone"]["reachable"] = True
        except MicrophoneDeniedError:
            report["microphone"]["permission_denied"] = True
        except Exception as exc:
            report["microphone"]["error"] = str(exc)
    return report


def main(argv: list[str] | None = None) -> int:
    report = check_environment()
    print(json.dumps(report, indent=2))
    missing = [
        name for name, present in report["dependencies"].items() if not present
    ]
    missing_models = [
        name for name, configured in report["models"].items() if not configured
    ]
    missing_data = [name for name, present in report["data"].items() if not present]
    if report["microphone"].get("permission_denied"):
        print("Microphone permission denied — grant it in System Settings > Privacy & Security.", file=sys.stderr)
        return 3
    if missing or missing_models:
        print(
            f"missing dependencies: {', '.join(missing) or 'none'}; "
            f"unconfigured models: {', '.join(missing_models) or 'none'}",
            file=sys.stderr,
        )
        if (
            report["models"].get("wake_head_configured")
            and not report["models"].get("wake_head")
        ):
            print(
                "wake head configured at "
                f"{report['models'].get('wake_head_path')!r} but missing on disk — "
                "ears will fall back to API-side EVIE spotting (slower). Fix: train/"
                "export the head with `uv run python -m clients.ears.train.train_head "
                " --positive-dir ...` and place the .onnx there.",
                file=sys.stderr,
            )
        return 1
    if missing_data:
        print(
            f"missing data for acceptance gates: {', '.join(missing_data)}",
            file=sys.stderr,
        )
        return 2
    print("ears stack ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
