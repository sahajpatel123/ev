"""AGENT 7 ROSTER CLI: ``python -m app.people.cli ...``.

Enrollment is a human-in-the-loop flow: point at >=5 aligned crops of one
consenting person, name them, grant/confirm consent, and EV stores an encrypted
mean template plus encrypted per-sample templates. Recognition only ever
matches against enrolled templates; anything else resolves to ``unknown``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.people.biodata import BiodataError, BiodataResolver
from app.people.calibration import apply_threshold, calibrate
from app.people.enrollment import FaceEnrollmentService
from app.people.erasure import erase_person
from app.people.errors import FaceError
from app.people.face_embed import FaceCrop
from app.people.resolver import FaceResolver


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _image_files(directory: Path) -> list[Path]:
    suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in suffixes
    )


def _crops_from_dir(directory: Path, quality: float, confidence: float) -> list[FaceCrop]:
    files = _image_files(directory)
    if len(files) < settings.face_min_photos:
        raise SystemExit(
            f"need >= {settings.face_min_photos} aligned crops, found {len(files)} in {directory}"
        )
    return [
        FaceCrop(
            image_b64=_b64(path.read_bytes()),
            quality=quality,
            confidence=confidence,
            source="cli",
        )
        for path in files
    ]


async def _grant_face_consent(reason: str | None) -> None:
    from app.db import SessionLocal
    from app.training import consent as consent_service

    async with SessionLocal() as session:
        await consent_service.grant_consent(
            session,
            track="face_enrollment",
            purpose="face enrollment from the owner's own photos with named consent",
            scope={"flow": "cli"},
            source="cli",
        )
        await session.commit()


async def cmd_enroll(args: argparse.Namespace) -> None:
    if args.grant_consent:
        await _grant_face_consent(args.reason)
    from app.db import SessionLocal
    from app.models import Entity

    crops = _crops_from_dir(Path(args.photos), args.quality, args.confidence)
    async with SessionLocal() as session:
        service = FaceEnrollmentService(session, master_key=settings.master_key)
        try:
            row = await service.enroll(
                person_name=args.name,
                photos=crops,
                reason=args.reason,
            )
        except FaceError as exc:
            await session.commit()
            raise SystemExit(f"enrollment failed [{exc.code}]: {exc.message}") from exc
        entity = await session.get(Entity, row.entity_id)
        await session.commit()
        payload = {
            "id": str(row.id),
            "entity_id": str(row.entity_id),
            "person_name": entity.name if entity is not None else args.name,
            "version": row.version,
            "is_current": row.is_current,
            "algorithm": row.algorithm,
            "embedding_dim": row.embedding_dim,
            "threshold": row.threshold,
            "sample_count": row.sample_count,
            "status": row.status,
            "consent_id": str(row.consent_id) if row.consent_id else None,
            "provider": service.embedder.name,
            "degraded": service.embedder.degraded,
            "raw_photos_stored": False,
        }
        print(json.dumps(payload, indent=2))


async def cmd_list(_: argparse.Namespace) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Entity, FaceEnrollment

    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(FaceEnrollment).order_by(FaceEnrollment.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        out = []
        for row in rows:
            entity = await session.get(Entity, row.entity_id)
            out.append(
                {
                    "id": str(row.id),
                    "entity_id": str(row.entity_id),
                    "person_name": entity.name if entity is not None else "?",
                    "version": row.version,
                    "is_current": row.is_current,
                    "status": row.status,
                    "sample_count": row.sample_count,
                    "algorithm": row.algorithm,
                }
            )
        print(json.dumps(out, indent=2))


async def cmd_recognize(args: argparse.Namespace) -> None:
    from app.db import SessionLocal

    image = Path(args.image).read_bytes()
    async with SessionLocal() as session:
        resolver = FaceResolver(session, master_key=settings.master_key)
        crop = FaceCrop(
            image_b64=_b64(image),
            quality=args.quality,
            confidence=args.confidence,
            source=args.source,
        )
        try:
            result = await resolver.recognize(crop, write_log=not args.no_log)
        except FaceError as exc:
            await session.commit()
            raise SystemExit(f"recognition failed [{exc.code}]: {exc.message}") from exc
        await session.commit()
        print(
            json.dumps(
                {
                    "resolved": result.resolved,
                    "unknown": result.unknown,
                    "label": result.label,
                    "entity_id": str(result.entity_id) if result.entity_id else None,
                    "confidence": result.confidence,
                    "threshold": result.threshold,
                    "provider": result.provider,
                    "degraded": result.degraded,
                    "recognition_id": str(result.recognition_id)
                    if result.recognition_id
                    else None,
                    "candidates": result.candidates[:5],
                },
                indent=2,
            )
        )


async def cmd_calibrate(args: argparse.Namespace) -> None:
    from app.db import SessionLocal

    manifest = json.loads(Path(args.manifest).read_text())
    trials = []
    for trial in manifest.get("trials", []):
        images = [
            _b64(Path(path).read_bytes()) for path in trial.get("images", [])
        ]
        if len(images) < 2:
            raise SystemExit(f"trial {trial.get('person')!r} needs >=2 images")
        trials.append({"person": trial["person"], "images": images})
    async with SessionLocal() as session:
        report = await calibrate(trials, target_far=args.target_far)
        if args.apply:
            await apply_threshold(
                session,
                master_key=settings.master_key,
                threshold=report.threshold,
            )
        await session.commit()
        print(report.model_dump_json(indent=2))


async def cmd_delete_enrollment(args: argparse.Namespace) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        service = FaceEnrollmentService(session, master_key=settings.master_key)
        try:
            row = await service.delete(UUID(args.enrollment_id), reason=args.reason)
        except FaceError as exc:
            await session.commit()
            raise SystemExit(f"delete failed [{exc.code}]: {exc.message}") from exc
        await session.commit()
        print(json.dumps({"id": str(row.id), "status": row.status}, indent=2))


async def cmd_erase_person(args: argparse.Namespace) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        manifest = await erase_person(
            session,
            entity_id=UUID(args.entity_id),
            reason=args.reason,
            actor="cli",
        )
        await session.commit()
        print(json.dumps(manifest, indent=2))


async def cmd_biodata(args: argparse.Namespace) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        resolver = BiodataResolver(session)
        try:
            result = await resolver.resolve(args.name)
        except BiodataError as exc:
            await session.commit()
            raise SystemExit(f"biodata failed [{exc.code}]: {exc.message}") from exc
        await session.commit()
        out = await resolver.to_schema(result)
        print(out.model_dump_json(indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.people.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enroll", help="enroll one person from aligned crops")
    p.add_argument("--name", required=True)
    p.add_argument("--photos", required=True, help="directory of aligned crops")
    p.add_argument("--quality", type=float, required=True, help="detector quality 0..1")
    p.add_argument("--confidence", type=float, required=True, help="detector confidence 0..1")
    p.add_argument("--reason")
    p.add_argument("--grant-consent", action="store_true")
    p.set_defaults(func=cmd_enroll)

    sub.add_parser("list", help="list face enrollments").set_defaults(func=cmd_list)

    p = sub.add_parser("recognize", help="match one aligned crop against enrolled people")
    p.add_argument("--image", required=True)
    p.add_argument("--quality", type=float)
    p.add_argument("--confidence", type=float)
    p.add_argument("--source", default="cli")
    p.add_argument("--no-log", action="store_true")
    p.set_defaults(func=cmd_recognize)

    p = sub.add_parser("calibrate", help="ROC-calibrate threshold from labeled trials")
    p.add_argument("--manifest", required=True, help="JSON: {trials:[{person, images:[paths]}]}")
    p.add_argument("--target-far", type=float, default=1e-3)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("delete-enrollment", help="delete one enrollment (audit row remains)")
    p.add_argument("--enrollment-id", required=True)
    p.add_argument("--reason", default="user deleted")
    p.set_defaults(func=cmd_delete_enrollment)

    p = sub.add_parser("erase-person", help="per-person erasure of every trace")
    p.add_argument("--entity-id", required=True)
    p.add_argument("--reason", default="user requested person deletion")
    p.set_defaults(func=cmd_erase_person)

    p = sub.add_parser("biodata", help="licensed public-figure biodata by name")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_biodata)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
