"""AGENT 7 ROSTER acceptance-harness tests (held-out ROC + stranger gate)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.people.eval import evaluate, far_at, held_out_split, tar_at
from app.people.face_embed import FaceCrop, FaceEmbeddingResult


class SyntheticEvalEmbedder:
    """Deterministic stand-in embedder for harness mechanics (NOT face recognition).

    Each person maps to a stable unit vector; enrollment and held-out crops of
    the same person therefore match at cosine 1.0 and different people at ~0.
    This only exercises the evaluation harness; real gates use SFace.
    """

    name = "synthetic-eval"
    embedding_dim = 64
    degraded = True

    async def embed(self, crop: FaceCrop) -> FaceEmbeddingResult:
        digest = hashlib.sha256(crop.source.encode("utf-8")).digest()
        vector = [0.0] * self.embedding_dim
        vector[digest[0] % self.embedding_dim] = 1.0
        return FaceEmbeddingResult(
            embedding=vector,
            quality=crop.quality,
            confidence=crop.confidence,
            provider=self.name,
            degraded=True,
            model="synthetic",
        )

def _make_people(tmp_path: Path, people: int = 5, photos: int = 10) -> Path:
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    for person in range(people):
        person_dir = people_dir / f"Person{person}"
        person_dir.mkdir()
        for photo in range(photos):
            (person_dir / f"{photo:02d}.png").write_bytes(
                f"person{person}-photo{photo}".encode() * 40
            )
    return people_dir


def _make_strangers(tmp_path: Path, count: int = 50) -> Path:
    strangers_dir = tmp_path / "strangers"
    strangers_dir.mkdir()
    for index in range(count):
        (strangers_dir / f"stranger-{index:02d}.png").write_bytes(
            f"stranger-{index}".encode() * 40
        )
    return strangers_dir


def test_held_out_split_is_deterministic_and_balanced() -> None:
    genuine = [0.9, 0.8, 0.7, 0.95, 0.85, 0.75]
    impostor = [0.1, 0.2, 0.05, 0.15, 0.25, 0.0]
    first = held_out_split(genuine, impostor, seed=7)
    second = held_out_split(genuine, impostor, seed=7)
    assert first == second
    assert len(first["genuine_train"]) == 3
    assert len(first["genuine_test"]) == 3
    assert len(first["impostor_train"]) == 3
    assert len(first["impostor_test"]) == 3


def test_tar_far_helpers() -> None:
    assert tar_at(0.5, [0.9, 0.8, 0.4]) == pytest.approx(2 / 3)
    assert far_at(0.5, [0.1, 0.6, 0.7]) == pytest.approx(2 / 3)
    assert tar_at(1.0, []) == 0.0


async def test_evaluate_acceptance_gate_with_synthetic_embedder(
    tmp_path: Path,
) -> None:
    people_dir = _make_people(tmp_path)
    strangers_dir = _make_strangers(tmp_path)
    report = await evaluate(
        people_dir=people_dir,
        strangers_dir=strangers_dir,
        target_far=1e-3,
        min_photos=10,
        eval_per_person=2,
        quality=0.9,
        confidence=0.9,
        grant_consent=True,
        embedder=SyntheticEvalEmbedder(),
    )
    assert report["people_enrolled"] == 5
    assert report["photos_per_person"] == 10
    assert report["enrollment_photos_per_person"] == 8
    assert report["held_out_eval_photos_per_person"] == 2
    assert report["genuine_pairs"] == 10
    assert report["impostor_pairs"] == 40
    assert report["tar_held_out"] == 1.0
    assert report["far_held_out"] == 0.0
    assert report["strangers_total"] == 50
    assert report["strangers_unknown"] == 50
    assert report["stranger_rejection_rate"] == 1.0
    assert report["acceptance_tar_met"] is True
    assert report["acceptance_rejection_met"] is True
    assert report["roc_train"] and report["roc_test"]
    assert report["provider"] == "synthetic-eval"
    assert report["degraded"] is True


async def test_evaluate_requires_explicit_consent(tmp_path: Path) -> None:
    people_dir = _make_people(tmp_path)
    strangers_dir = _make_strangers(tmp_path)
    with pytest.raises(RuntimeError, match="consent"):
        await evaluate(
            people_dir=people_dir,
            strangers_dir=strangers_dir,
            min_photos=10,
            quality=0.9,
            confidence=0.9,
            grant_consent=False,
            embedder=SyntheticEvalEmbedder(),
        )


async def test_evaluate_refuses_too_few_people_or_strangers(tmp_path: Path) -> None:
    people_dir = _make_people(tmp_path, people=4)
    strangers_dir = _make_strangers(tmp_path, count=10)
    with pytest.raises(ValueError, match="5 people"):
        await evaluate(
            people_dir=people_dir,
            strangers_dir=strangers_dir,
            quality=0.9,
            confidence=0.9,
            grant_consent=True,
            embedder=SyntheticEvalEmbedder(),
        )
