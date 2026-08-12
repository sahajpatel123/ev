"""ev-eval console script: canonical artifacts and skip semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import cli


@pytest.fixture
def ml_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "ml"
    monkeypatch.setattr(cli, "ML_EVAL_DIR", target)
    return target


def test_all_dry_run_writes_nothing_and_prints_gate_reasons(
    ml_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "retrieval: would write" in out
    assert "asr: would write" in out
    assert "wake: would write" in out
    assert "no eval artifact at" in out
    assert not list(ml_dir.glob("*.json"))


def test_asr_falls_back_to_gate_skip_when_harness_absent(
    ml_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def blocked(name: str) -> object:
        raise ImportError("harness not landed")

    monkeypatch.setattr(cli.importlib, "import_module", blocked)
    assert cli.main(["asr"]) == 0
    out = capsys.readouterr().out
    assert "no eval artifact at" in out
    assert "run Agent 4's ASR eval" in out
    assert not (ml_dir / "asr_quality.json").exists()


def test_asr_runs_agent4_harness_and_canonicalizes(
    ml_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.ml import asr_eval

    def fake_asr_main(argv: list[str]) -> int:
        asr_eval.OUT_PATH.write_text(
            json.dumps(
                {
                    "dataset": "librispeech dummy",
                    "provider": "parakeet-eou-120m",
                    "samples": 10,
                    "measured": True,
                    "wer_mean": 0.07,
                    "wer_clean": 0.07,
                    "wer_owner_speech": 0.10,
                    "wer_samples": [],
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(asr_eval, "main", fake_asr_main)
    assert cli.main(["asr"]) == 0
    artifact = ml_dir / "asr_quality.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["schema"] == "ev.asr.eval.v1"
    assert data["producer"] == "ev-eval"
    assert data["degraded"] is False
    assert data["wer_clean"] == 0.07


def test_asr_unmeasured_run_is_marked_degraded(
    ml_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.ml import asr_eval

    def fake_asr_main(argv: list[str]) -> int:
        asr_eval.OUT_PATH.write_text(
            json.dumps({"measured": False, "error": "provider unavailable"}),
            encoding="utf-8",
        )
        return 2

    monkeypatch.setattr(asr_eval, "main", fake_asr_main)
    assert cli.main(["asr"]) == 2
    data = json.loads((ml_dir / "asr_quality.json").read_text(encoding="utf-8"))
    assert data["degraded"] is True


def test_asr_harness_failure_records_degraded_artifact(
    ml_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.ml import asr_eval

    def failing_main(argv: list[str]) -> int:
        raise SystemExit("datasets is not installed")

    monkeypatch.setattr(asr_eval, "main", failing_main)
    assert cli.main(["asr"]) == 2
    data = json.loads((ml_dir / "asr_quality.json").read_text(encoding="utf-8"))
    assert data["measured"] is False
    assert data["degraded"] is True
    assert "datasets is not installed" in data["error"]


def test_wake_skips_with_exact_gate_reason(
    ml_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["wake"]) == 0
    out = capsys.readouterr().out
    assert "no eval artifact at" in out
    assert "false_accepts_per_12h" in out
    assert not (ml_dir / "wake_reliability.json").exists()


def test_wake_runs_agent3_harness_and_canonicalizes(
    ml_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.audio import wake_eval

    def fake_wake_main(argv: list[str]) -> int:
        report = Path(argv[argv.index("--report") + 1])
        report.write_text(
            json.dumps(
                {
                    "provider": "openwakeword",
                    "degraded": False,
                    "false_accepts_per_12h": 0.0,
                    "recall": 0.95,
                    "hours_audio": 12.0,
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(wake_eval, "main", fake_wake_main)
    assert (
        cli.main(["wake", "--held-out-dir", "clips", "--ambient", "room.wav", "--model-path", "head.onnx"])
        == 0
    )
    artifact = ml_dir / "wake_reliability.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["schema"] == "ev.wake.eval.v1"
    assert data["producer"] == "ev-eval"
    assert data["recall"] == 0.95


def test_speaker_skips_without_data_dirs(
    ml_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["speaker"]) == 0
    out = capsys.readouterr().out
    assert "no eval artifact at" in out
    assert "app.voice.speaker eval" in out
    assert not (ml_dir / "speaker_security.json").exists()


def test_face_skips_without_data_dirs(
    ml_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["face"]) == 0
    out = capsys.readouterr().out
    assert "no eval artifact at" in out
    assert "app.people.eval" in out
    assert not (ml_dir / "face_recognition.json").exists()


def test_retrieval_writes_canonical_artifact(
    ml_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.retrieval import cli as retrieval_cli

    def fake_retrieval_main(argv: list[str]) -> int:
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(
            json.dumps(
                {
                    "schema_version": "ev.retrieval.eval.v1",
                    "before_after": {
                        "provider": {
                            "provider": "granite",
                            "degraded": False,
                            "ndcg_at_10": 0.98,
                            "top5_hit_rate": 0.98,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(retrieval_cli, "main", fake_retrieval_main)
    assert cli.main(["retrieval"]) == 0
    artifact = ml_dir / "retrieval_quality.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["schema"] == "ev.retrieval.eval.v1"
    assert data["schema_version"] == "ev.retrieval.eval.v1"
    assert data["producer"] == "ev-eval"
    assert data["before_after"]["provider"]["ndcg_at_10"] == 0.98


def test_all_runs_available_and_skips_rest(
    ml_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.audio import wake_eval
    from eval.ml import asr_eval
    from eval.retrieval import cli as retrieval_cli

    def fake_retrieval_main(argv: list[str]) -> int:
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(
            json.dumps(
                {
                    "before_after": {
                        "provider": {
                            "provider": "hash",
                            "degraded": True,
                            "ndcg_at_10": 0.6,
                            "top5_hit_rate": 0.7,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(retrieval_cli, "main", fake_retrieval_main)

    def fake_asr_main(argv: list[str]) -> int:
        asr_eval.OUT_PATH.write_text(
            json.dumps({"measured": False, "error": "offline"}),
            encoding="utf-8",
        )
        return 2

    monkeypatch.setattr(asr_eval, "main", fake_asr_main)

    def fake_wake_main(argv: list[str]) -> int:
        report = Path(argv[argv.index("--report") + 1])
        report.write_text(
            json.dumps(
                {
                    "provider": "openwakeword",
                    "degraded": True,
                    "false_accepts_per_12h": 0.0,
                    "recall": 0.95,
                    "hours_audio": 12.0,
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(wake_eval, "main", fake_wake_main)
    assert (
        cli.main(
            [
                "all",
                "--held-out-dir",
                "clips",
                "--ambient",
                "room.wav",
                "--model-path",
                "head.onnx",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "retrieval: report written to" in out
    assert "asr: report written to" in out
    assert "wake: report written to" in out
    assert out.count("no eval artifact at") == 2  # speaker, face
    assert (ml_dir / "retrieval_quality.json").exists()
    assert (ml_dir / "asr_quality.json").exists()
    assert (ml_dir / "wake_reliability.json").exists()
    assert json.loads((ml_dir / "asr_quality.json").read_text(encoding="utf-8"))["degraded"] is True
