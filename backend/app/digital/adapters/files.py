"""Bounded artifacts across Gmail / WhatsApp / phone / browser downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb


class FileServiceAdapter:
    slug = "files"
    display_name = "File / attachment fabric"

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        common = dict(
            credential_type="local_store",
            data_classification="external_file",
            backing="EV storage (quarantined)",
        )
        return [
            cap("files", "save", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="sha256", **common),
            cap("files", "read_meta", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="sha256", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        if operation == "save":
            blob: bytes = args.get("bytes") or b""
            if isinstance(blob, str):
                blob = blob.encode("utf-8")
            if _looks_executable(str(args.get("name") or ""), args.get("mime")):
                return OpResult(
                    status=OpStatus.BLOCKED,
                    service="files",
                    operation=operation,
                    availability=Availability.NATIVE,
                    error="executable_quarantined",
                    diagnosis="untrusted_attachment",
                    payload={"quarantined": True, "executed": False},
                )
            stored = save_artifact(
                blob,
                name=str(args.get("name") or "unnamed"),
                mime=str(args.get("mime") or "application/octet-stream"),
                source=str(args.get("source") or "unknown"),
                provenance=args.get("provenance") or {},
            )
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="files",
                operation=operation,
                availability=Availability.NATIVE,
                payload=stored,
                verification={"sha256": stored["sha256"], "size": stored["size"]},
            )
        if operation == "read_meta":
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="files",
                operation=operation,
                availability=Availability.NATIVE,
                payload={"path": args.get("path"), "origin": "EXTERNAL_CONTENT"},
            )
        return OpResult(status=OpStatus.FAILED, service="files", operation=operation,
                        availability=Availability.NATIVE, error="unknown_operation")


def save_artifact(
    blob: bytes,
    *,
    name: str,
    mime: str,
    source: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    digest = hashlib.sha256(blob).hexdigest()
    root = Path(getattr(settings, "digital_artifact_dir", None) or "./storage/digital-artifacts")
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{digest[:16]}-{name.replace('/', '_')}"
    if not dest.exists():
        dest.write_bytes(blob)
    return {
        "id": str(uuid4()),
        "name": name,
        "mime": mime,
        "size": len(blob),
        "sha256": digest,
        "path": str(dest),
        "source": source,
        "provenance": provenance,
        "quarantined": True,
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
        "executed": False,
    }


def _looks_executable(name: str, mime: Any) -> bool:
    lowered = name.lower()
    if lowered.endswith((".exe", ".dmg", ".pkg", ".bat", ".cmd", ".scr", ".js", ".vbs", ".command")):
        return True
    return str(mime or "").lower() in {"application/x-msdownload", "application/x-executable", "application/x-sh"}
