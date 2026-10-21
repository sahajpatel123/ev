"""ev models: status shape + report counting (smokes run live, not here)."""

from __future__ import annotations


def test_models_status_shape() -> None:
    from clients.cli import models as models_mod

    st = models_mod.status()
    assert set(st["dependencies"]) >= {"onnxruntime", "sounddevice",
                                       "faster_whisper", "openwakeword",
                                       "kokoro_onnx", "edge_tts"}
    assert set(st["weights"]) >= {"openwakeword_head", "faster_whisper_tiny",
                                  "kokoro_int8"}
    assert set(st["configured"]) >= {"wake", "asr", "tts"}
    assert isinstance(st["microphone"].get("reachable"), bool)


def test_models_report_counts_failures(capsys) -> None:
    from clients.cli.models import print_report

    code = print_report({"smoke": {"a": {"status": "PASS", "detail": "x"},
                                   "b": {"status": "FAIL", "detail": "y"}},
                         "weights": {"w": "present"}})
    assert code == 1
    out = capsys.readouterr().out
    assert "[PASS] smoke:a" in out and "[FAIL] smoke:b" in out


def test_models_parser() -> None:
    from clients.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["models"]).models_command == "status"
    assert parser.parse_args(["models", "install", "--fetch-all"]).fetch_all is True
