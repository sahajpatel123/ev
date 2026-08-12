"""Agent 6 local perception tests: detector/scene/face doubles + real factory."""

from __future__ import annotations

import json
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import vision
from app.vision import detect as detect_module
from app.vision import eval as eval_module
from app.vision import face as face_module
from app.vision import scene as scene_module
from app.vision.detect import DetectionResult, create_detector
from app.vision.face import FaceDetectionResult, create_face_detector
from app.vision.scene import create_scene_encoder


class StubSession:
    """Minimal ONNX-session stand-in returning canned outputs (test only)."""

    def __init__(self, outputs: list) -> None:
        self.outputs = outputs

    def run(self, feed_names, inputs) -> list:
        return self.outputs


async def upload_attachment(
    client: AsyncClient,
    *,
    content: bytes = b"fake-image-bytes",
    content_type: str = "image/png",
    filename: str = "photo.png",
    metadata: dict | None = None,
    privacy_level: str = "normal",
) -> str:
    resp = await client.post(
        "/v1/attachments",
        files={"file": (filename, content, content_type)},
        data={
            "metadata": json.dumps(metadata or {}),
            "privacy_level": privacy_level,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["attachment"]["id"]


async def test_detector_double_is_degraded_and_empty() -> None:
    detector = create_detector(engine="double")
    result = await detector.detect(b"not-an-image", "image/png")
    assert result.objects == []
    assert result.degraded is True
    assert result.engine == "deterministic"


async def test_detector_real_factory_parses_rtdetr_output(monkeypatch) -> None:
    outputs = [
        [1],
        [[[0.0, 0.0, 0.5, 0.5]]],
        [[0.93]],
        [[0]],
    ]
    monkeypatch.setattr(detect_module, "_preprocess_image", lambda data: {"images": [[0.0]]})
    detector = create_detector(engine="onnx", session=StubSession(outputs))
    result = await detector.detect(b"image-bytes", "image/png")
    assert result.degraded is False
    assert result.engine == "onnx"
    assert len(result.objects) == 1
    obj = result.objects[0]
    assert obj["label"] == "person"
    assert obj["confidence"] == 0.93
    assert obj["bounding_box"]["width"] == 0.5


async def test_scene_double_is_degraded_and_empty() -> None:
    encoder = create_scene_encoder(engine="double")
    result = await encoder.encode_scene(b"not-an-image")
    assert result.labels == []
    assert result.embedding is None
    assert result.degraded is True


async def test_scene_real_factory_scores_candidates(monkeypatch) -> None:
    monkeypatch.setattr(scene_module, "_preprocess_image", lambda data: {"pixel_values": [0.0]})
    monkeypatch.setattr(
        scene_module,
        "ENCODE_CANDIDATES_HOOK",
        lambda candidates, session, name: [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    encoder = create_scene_encoder(
        engine="onnx",
        session=StubSession([[[1.0, 0.0, 0.0]]]),
        text_session=StubSession([]),
    )
    result = await encoder.encode_scene(b"image-bytes", ["person", "car"])
    assert result.degraded is False
    assert result.embedding == [1.0, 0.0, 0.0]
    assert [item["label"] for item in result.labels] == ["person"]
    assert result.labels[0]["confidence"] == 1.0


async def test_face_double_is_degraded_and_empty() -> None:
    detector = create_face_detector(engine="double")
    result = await detector.detect(b"not-an-image", "image/png")
    assert result.faces == []
    assert result.degraded is True


async def test_face_real_factory_parses_yunet_output(monkeypatch) -> None:
    yunet_row = [
        0.1, 0.1, 0.5, 0.5,
        0.2, 0.2, 0.6, 0.2, 0.4, 0.45, 0.25, 0.65, 0.55, 0.65,
        0.95,
    ]
    monkeypatch.setattr(face_module, "_preprocess_image", lambda data: {"input": [0.0]})
    detector = create_face_detector(engine="onnx", session=StubSession([[[yunet_row]]]))
    result = await detector.detect(b"image-bytes", "image/png")
    assert result.degraded is False
    assert len(result.faces) == 1
    face = result.faces[0]
    assert face["bounding_box"]["x"] == 0.1
    assert len(face["landmarks"]) == 5
    assert face["score"] == 0.95
    assert isinstance(face["alignment_angle"], float)


class FakeDetector:
    name = "test"
    engine = "test"

    async def detect(self, data, content_type=None) -> DetectionResult:
        return DetectionResult(
            objects=[
                {
                    "label": "workbench",
                    "confidence": 0.9,
                    "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
                    "class_id": -1,
                }
            ],
            degraded=False,
            engine="test",
        )


class FakeFaceDetector:
    name = "test-face"
    engine = "test-face"

    async def detect(self, data, content_type=None) -> FaceDetectionResult:
        return FaceDetectionResult(
            faces=[
                {
                    "bounding_box": {"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.3},
                    "landmarks": [{"x": 0.3, "y": 0.3}, {"x": 0.4, "y": 0.3}],
                    "alignment_angle": 0.0,
                    "score": 0.99,
                }
            ],
            degraded=False,
            engine="test-face",
        )


async def test_local_detection_suggestions_are_pending_then_confirmed(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from app.gateway.providers import MockProvider

    monkeypatch.setattr(detect_module, "create_detector", lambda *a, **k: FakeDetector())
    attachment_id = await upload_attachment(
        client,
        metadata={"derived_text": "scene with a workbench"},
    )
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=False,
        provider=MockProvider(),
    )
    await db_session.commit()

    payload = row.payload
    assert {"label": "workbench", "confidence": 0.9} in payload["labels"]
    assert payload["local_engines"]["detect"] == "test"
    # Scene/face remain offline doubles in CI, so the local layer is degraded
    # even though the detector engine is real (test stub).
    assert payload["local_degraded"] is True

    suggestions = [
        r for r in (await client.get("/v1/vision/log")).json() if r["source"] == "model"
    ]
    target = next(r for r in suggestions if r["label"] == "workbench")
    assert target["entity_id"] is None

    resp = await client.post(
        f"/v1/vision/recognitions/{target['id']}/confirm",
        json={"entity_type": "thing"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "user"
    assert resp.json()["entity_id"] is not None


async def test_face_boxes_never_persisted_in_perception_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from app.gateway.providers import MockProvider

    monkeypatch.setattr(face_module, "create_face_detector", lambda *a, **k: FakeFaceDetector())
    attachment_id = await upload_attachment(client)
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=False,
        provider=MockProvider(),
    )
    await db_session.commit()

    payload = row.payload
    assert payload["face_detections"][0]["count"] == 1
    assert "bounding_box" not in json.dumps(payload)
    assert "landmarks" not in json.dumps(payload)


class SequenceDetector:
    """Returns one DetectionResult per call (test fixture)."""

    name = "sequence"

    def __init__(self, outputs: list[list[dict]]) -> None:
        self.outputs = outputs
        self.calls = 0

    async def detect(self, data, content_type=None) -> DetectionResult:
        output = self.outputs[self.calls]
        self.calls += 1
        return DetectionResult(objects=output, degraded=False, engine="sequence")


async def test_map_proxy_perfect_detector_scores_1_0() -> None:
    person = {
        "label": "person",
        "confidence": 0.99,
        "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
        "class_id": 0,
    }
    car = {
        "label": "car",
        "confidence": 0.95,
        "bounding_box": {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.3},
        "class_id": 2,
    }
    images = [
        (b"image-1", [{"class_id": 0, "bbox": [0.1, 0.1, 0.5, 0.5]}]),
        (b"image-2", [{"class_id": 2, "bbox": [0.2, 0.2, 0.4, 0.3]}]),
    ]
    result = await eval_module.run_spot_check(
        SequenceDetector([[person], [car]]),
        images,
    )
    assert result.map_score == 1.0
    assert result.per_class["0"]["tp"] == 1
    assert result.per_class["2"]["tp"] == 1
    assert result.per_class["0"]["fp"] == 0
    assert result.degraded is False


async def test_map_proxy_wrong_class_scores_zero() -> None:
    wrong = {
        "label": "person",
        "confidence": 0.99,
        "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
        "class_id": 0,
    }
    images = [(b"image-1", [{"class_id": 2, "bbox": [0.1, 0.1, 0.5, 0.5]}])]
    result = await eval_module.run_spot_check(SequenceDetector([[wrong]]), images)
    assert result.map_score == 0.0
    assert result.per_class["2"]["tp"] == 0
    # The wrong-class prediction is an FP for the predicted class and the
    # ground-truth class is left unmatched (FN).
    assert result.per_class["0"]["fp"] == 1
    assert result.per_class["2"]["fn"] == 1
    assert result.per_class["2"]["ap"] == 0.0


async def test_map_proxy_double_is_degraded() -> None:
    images = [(b"image-1", [{"class_id": 0, "bbox": [0.0, 0.0, 0.5, 0.5]}])]
    result = await eval_module.run_spot_check(
        detect_module.DeterministicDetector(),
        images,
    )
    assert result.map_score == 0.0
    assert result.degraded is True


def test_load_coco_spotcheck(tmp_path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "one.png").write_bytes(b"fake-png")
    (tmp_path / "annotations.json").write_text(
        json.dumps(
            {
                "categories": [{"id": 0, "name": "person"}],
                "images": [{"id": 1, "file_name": "images/one.png"}],
                "annotations": [
                    {"image_id": 1, "category_id": 0, "bbox": [0.1, 0.1, 0.5, 0.5]}
                ],
            }
        )
    )
    loaded = eval_module.load_coco_spotcheck(tmp_path)
    assert len(loaded) == 1
    data, annotations = loaded[0]
    assert data == b"fake-png"
    assert annotations[0]["class_id"] == 0
    assert annotations[0]["class_name"] == "person"
