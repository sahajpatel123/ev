"""AGENT 7 ROSTER acceptance harness: held-out ROC + stranger-rejection gate.

Run when the owner's consent-approved photo set is available:

    EV_FACE_PROVIDER=sface uv run python -m app.people.eval \
        --people-dir ~/roster/people \
        --strangers-dir ~/roster/strangers \
        --quality 0.9 --confidence 0.95 --grant-consent

Directory layout:

* ``people/<Person Name>/*.jpg|png`` — aligned crops of one consented person
  (>= ``--min-photos`` per person, default 10).
* ``strangers/*.jpg|png`` — 50 photos of people who are NOT enrolled.

The harness performs a held-out split (last ``--eval-per-person`` photos per
person are never used to build the template), calibrates the cosine threshold
on a training half of the score pairs, reports TAR/FAR on the held-out half,
then runs every stranger through the production resolver and counts
``unknown``. Templates are Fernet-encrypted at rest in the same way as the
normal enrollment flow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from app.config import settings
from app.utils.text import utcnow

if TYPE_CHECKING:
    from app.people.face_embed import FaceEmbedder

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _image_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def held_out_split(
    genuine: list[float],
    impostor: list[float],
    *,
    seed: int = 7,
) -> dict[str, list[float]]:
    """Deterministic alternating split into training and held-out score halves."""
    rng = random.Random(seed)
    shuffled_g = list(genuine)
    shuffled_i = list(impostor)
    rng.shuffle(shuffled_g)
    rng.shuffle(shuffled_i)
    return {
        "genuine_train": shuffled_g[0::2],
        "genuine_test": shuffled_g[1::2],
        "impostor_train": shuffled_i[0::2],
        "impostor_test": shuffled_i[1::2],
    }


def tar_at(threshold: float, scores: list[float]) -> float:
    if not scores:
        return 0.0
    return sum(1.0 for score in scores if score >= threshold) / len(scores)


def far_at(threshold: float, scores: list[float]) -> float:
    return tar_at(threshold, scores)


async def evaluate(
    *,
    people_dir: Path,
    strangers_dir: Path,
    target_far: float = 1e-3,
    min_photos: int = 10,
    eval_per_person: int = 2,
    quality: float = 0.95,
    confidence: float = 0.98,
    grant_consent: bool = False,
    master_key: str | None = None,
    embedder: FaceEmbedder | None = None,
    reason: str = "acceptance evaluation",
) -> dict:
    """Run the full ROSTER acceptance gate and return a JSON-serializable report."""
    from app.db import Base, SessionLocal, engine
    from app.people.calibration import apply_threshold, roc_points, threshold_for_far
    from app.people.enrollment import FaceEnrollmentService
    from app.people.face_embed import FaceCrop, cosine_similarity, get_face_embedder
    from app.people.resolver import FaceResolver
    from app.training.consent import active_consent
    from app.training.consent import grant_consent as grant_consent_record

    embedder = embedder or get_face_embedder()
    master_key = master_key or settings.master_key

    person_dirs = sorted(
        path for path in people_dir.iterdir() if path.is_dir()
    )
    if len(person_dirs) < 5:
        raise ValueError(f"need >= 5 people directories, found {len(person_dirs)}")
    strangers = _image_files(strangers_dir)
    if len(strangers) < 50:
        raise ValueError(
            f"need >= 50 stranger photos for the rejection gate, found {len(strangers)}"
        )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        consent = await active_consent(session, "face_enrollment")
        if consent is None:
            if not grant_consent:
                raise RuntimeError(
                    "face_enrollment consent required for evaluation; "
                    "pass --grant-consent to record it"
                )
            await grant_consent_record(
                session,
                track="face_enrollment",
                purpose=reason,
                scope={"flow": "acceptance_eval"},
                source="cli",
            )

        service = FaceEnrollmentService(session, master_key=master_key, embedder=embedder)
        plan: list[tuple[str, UUID, list[Path]]] = []
        for person_dir in person_dirs:
            files = _image_files(person_dir)
            if len(files) < min_photos:
                raise ValueError(
                    f"{person_dir.name!r} has {len(files)} photos; "
                    f"need >= {min_photos}"
                )
            enroll_files = files[: len(files) - eval_per_person]
            eval_files = files[len(files) - eval_per_person :]
            photos = [
                FaceCrop(
                    image_b64=_b64(path),
                    quality=quality,
                    confidence=confidence,
                    source=f"eval:{person_dir.name}",
                )
                for path in enroll_files
            ]
            row = await service.enroll(
                person_name=person_dir.name,
                photos=photos,
                reason=reason,
            )
            plan.append((person_dir.name, row.entity_id, eval_files))
        await session.flush()

        payloads = {
            str(entity.id): payload
            for entity, payload in await service.current_payloads()
        }
        person_by_entity = {
            str(entity_id): person_name for person_name, entity_id, _ in plan
        }

        genuine: list[float] = []
        impostor: list[float] = []
        eval_embeddings: dict[tuple[str, Path], list[float]] = {}
        for person_name, entity_id, eval_files in plan:
            template = list(payloads[str(entity_id)]["embedding"])
            for path in eval_files:
                crop = FaceCrop(
                    image_b64=_b64(path),
                    quality=quality,
                    confidence=confidence,
                    source=f"eval:{person_name}",
                )
                embedding = (await embedder.embed(crop)).embedding
                eval_embeddings[(person_name, path)] = embedding
                genuine.append(cosine_similarity(embedding, template))

        for (person_name, _), embedding in eval_embeddings.items():
            for other_entity, payload in payloads.items():
                if person_by_entity[other_entity] == person_name:
                    continue
                impostor.append(
                    cosine_similarity(embedding, list(payload["embedding"]))
                )

        split = held_out_split(genuine, impostor)
        threshold, tar_train, roc_train = threshold_for_far(
            split["genuine_train"],
            split["impostor_train"],
            target_far=target_far,
        )
        tar_held_out = tar_at(threshold, split["genuine_test"])
        far_held_out = far_at(threshold, split["impostor_test"])
        roc_test = roc_points(split["genuine_test"], split["impostor_test"])

        await apply_threshold(
            session,
            master_key=master_key,
            threshold=threshold,
        )
        await session.flush()

        resolver = FaceResolver(session, master_key=master_key, embedder=embedder)
        strangers_unknown = 0
        for path in strangers:
            crop = FaceCrop(
                image_b64=_b64(path),
                quality=quality,
                confidence=confidence,
                source="eval:stranger",
            )
            result = await resolver.recognize(crop, write_log=False)
            if result.unknown:
                strangers_unknown += 1

        report = {
            "provider": embedder.name,
            "degraded": embedder.degraded,
            "people_enrolled": len(person_dirs),
            "photos_per_person": len(_image_files(person_dirs[0])) if person_dirs else 0,
            "enrollment_photos_per_person": (
                len(_image_files(person_dirs[0])) - eval_per_person if person_dirs else 0
            ),
            "held_out_eval_photos_per_person": eval_per_person,
            "genuine_pairs": len(genuine),
            "impostor_pairs": len(impostor),
            "threshold": threshold,
            "tar_train": tar_train,
            "far_train": far_at(threshold, split["impostor_train"]),
            "tar_held_out": tar_held_out,
            "far_held_out": far_held_out,
            "target_far": target_far,
            "roc_train": roc_train,
            "roc_test": roc_test,
            "strangers_total": len(strangers),
            "strangers_unknown": strangers_unknown,
            "stranger_rejection_rate": strangers_unknown / len(strangers),
            "acceptance_tar_met": tar_held_out >= 0.95,
            "acceptance_rejection_met": strangers_unknown == len(strangers),
            "ran_at": utcnow().isoformat(),
        }
        await session.commit()
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.people.eval")
    parser.add_argument("--people-dir", required=True, type=Path)
    parser.add_argument("--strangers-dir", required=True, type=Path)
    parser.add_argument("--target-far", type=float, default=1e-3)
    parser.add_argument("--min-photos", type=int, default=10)
    parser.add_argument("--eval-per-person", type=int, default=2)
    parser.add_argument("--quality", type=float, default=0.95)
    parser.add_argument("--confidence", type=float, default=0.98)
    parser.add_argument("--grant-consent", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = asyncio.run(
            evaluate(
                people_dir=args.people_dir,
                strangers_dir=args.strangers_dir,
                target_far=args.target_far,
                min_photos=args.min_photos,
                eval_per_person=args.eval_per_person,
                quality=args.quality,
                confidence=args.confidence,
                grant_consent=args.grant_consent,
            )
        )
    except (ValueError, RuntimeError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
