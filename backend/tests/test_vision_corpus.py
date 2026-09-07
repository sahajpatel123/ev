"""Tests for the deterministic 50-image detection spot-check corpus."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

from app.vision import corpus
from app.vision.eval import load_coco_spotcheck


@pytest.mark.skipif(
    corpus.CORPUS_CLASSES == [],
    reason="corpus module unavailable",
)
def test_corpus_generates_50_images_with_valid_coco_json(tmp_path) -> None:
    payload = corpus.generate_corpus(tmp_path)

    assert len(payload["images"]) == 50
    assert len(payload["annotations"]) >= 50
    assert len(payload["categories"]) == len(corpus.CORPUS_CLASSES)
    image_files = list((tmp_path / "images").glob("*.png"))
    assert len(image_files) == 50
    for image in payload["images"]:
        assert (tmp_path / image["file_name"]).exists()

    for annotation in payload["annotations"]:
        x, y, w, h = annotation["bbox"]
        assert 0.0 <= x < 1.0
        assert 0.0 <= y < 1.0
        assert 0.05 <= w <= 1.0
        assert 0.05 <= h <= 1.0
        assert x + w <= 1.0 + 1e-9
        assert y + h <= 1.0 + 1e-9
        assert 0 <= annotation["category_id"] < len(corpus.CORPUS_CLASSES)

    with (tmp_path / "annotations.json").open("r", encoding="utf-8") as handle:
        assert json.load(handle) == payload


def test_corpus_is_deterministic_and_reproducible(tmp_path) -> None:
    first = corpus.generate_corpus(tmp_path / "a", size=3, seed=42)
    second = corpus.generate_corpus(tmp_path / "b", size=3, seed=42)

    assert first == second
    assert corpus.corpus_sha256(tmp_path / "a") == corpus.corpus_sha256(tmp_path / "b")


def test_different_seeds_produce_different_corpus(tmp_path) -> None:
    first = corpus.generate_corpus(tmp_path / "a", size=3, seed=1)
    second = corpus.generate_corpus(tmp_path / "b", size=3, seed=2)

    assert first != second


def test_corpus_round_trips_through_coco_loader(tmp_path) -> None:
    payload = corpus.generate_corpus(tmp_path, size=5)

    loaded = load_coco_spotcheck(tmp_path)
    assert len(loaded) == 5
    total_gt = sum(len(annotations) for _, annotations in loaded)
    assert total_gt == len(payload["annotations"])
    first_bytes, first_gt = loaded[0]
    assert first_bytes.startswith(b"\x89PNG")
    assert set(first_gt[0]) >= {"class_id", "class_name", "bbox"}
