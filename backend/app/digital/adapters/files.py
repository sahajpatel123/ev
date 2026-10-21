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
        common: dict[str, Any] = dict(
            credential_type="local_store",
            data_classification="external_file",
            backing="EV storage",
        )
        return [
            cap("files", "save", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="sha256", **common),
            cap("files", "create", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="path_exists", **common),
            cap("files", "write", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="path_exists", **common),
            cap("files", "edit", Verb.UPDATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="path_exists", **common),
            cap("files", "append", Verb.UPDATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="path_exists", **common),
            cap("files", "read", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="path_exists", **common),
            cap("files", "read_meta", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="sha256", **common),
            cap("files", "summarize", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="deterministic_summary", **common),
            cap("files", "copy", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="path_exists", **common),
            cap("files", "move", Verb.MOVE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="path_exists", **common),
            cap("files", "delete", Verb.DELETE, Availability.NATIVE, read_write="write", risk="R2",
                verification_method="path_not_exists", **common),
            cap("files", "list", Verb.LIST, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="directory_listing", **common),
            cap("files", "search", Verb.SEARCH, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="index_hits", **common),
            cap("files", "reveal", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="os_finder", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        if operation == "save" and (args.get("bytes") is not None or "mime" in args and "content" not in args):
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
        from app.ev import file_sandbox

        if operation == "read_meta":
            receipt = file_sandbox.execute_op(
                "read_meta",
                {"path": str(args.get("path") or args.get("query") or "")},
                origin="digital:files",
            )
            return self._shape_receipt(operation, receipt)

        op_lower = operation.lower()
        # All local file VERBS route through the file sandbox: confirm gate,
        # jail, journal/undo. Never call laptop_files.perform_local directly —
        # that bypasses confirmation and leaves no journal entry.
        if op_lower in {"save", "create", "write"}:
            path = str(args.get("path") or args.get("name") or "")
            content = str(args.get("content") or args.get("text") or "")
            receipt = file_sandbox.execute_op(
                "write", {"path": path, "content": content},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "edit":
            receipt = file_sandbox.execute_op(
                "edit",
                {"path": str(args.get("path") or ""), "content": str(args.get("content") or args.get("text") or ""), "instruction": str(args.get("instruction") or "")},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "append":
            receipt = file_sandbox.execute_op(
                "append",
                {"path": str(args.get("path") or ""), "content": str(args.get("content") or args.get("text") or "")},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "read":
            path = str(args.get("path") or args.get("query") or "")
            receipt = file_sandbox.execute_op(
                "read", {"path": path},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "summarize":
            path = str(args.get("path") or "")
            query = str(args.get("query") or "")
            receipt = file_sandbox.execute_op(
                "summarize", {"path": path, "query": query},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "copy":
            path = str(args.get("path") or args.get("source") or "")
            dest = str(args.get("dest") or args.get("target") or args.get("destination") or "")
            receipt = file_sandbox.execute_op(
                "copy", {"path": path, "dest": dest},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "move":
            path = str(args.get("path") or args.get("source") or "")
            dest = str(args.get("dest") or args.get("target") or args.get("destination") or "")
            receipt = file_sandbox.execute_op(
                "move", {"path": path, "dest": dest},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "delete":
            receipt = file_sandbox.execute_op(
                "delete", {"path": str(args.get("path") or "")},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "list":
            path = str(args.get("path") or args.get("folder") or "")
            query = str(args.get("query") or "")
            receipt = file_sandbox.execute_op(
                "list", {"path": path, "query": query},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower == "search":
            query = str(args.get("query") or args.get("needle") or "")
            path = str(args.get("path") or "")
            receipt = file_sandbox.execute_op(
                "search", {"path": path, "query": query},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        if op_lower in {"reveal", "finder", "show_in_finder"}:
            path = str(args.get("path") or args.get("query") or "")
            receipt = file_sandbox.execute_op(
                "reveal", {"path": path},
                origin="digital:files", confirm=ctx.confirmed,
            )
            return self._shape_receipt(operation, receipt)

        return OpResult(
            status=OpStatus.FAILED,
            service="files",
            operation=operation,
            availability=Availability.NATIVE,
            error="unknown_operation",
        )

    def _shape_receipt(self, operation: str, receipt: dict[str, Any]) -> OpResult:
        # Sandbox receipts carry the confirm gate (needs_confirm) and the
        # journal/undo trail. needs_confirm → WAITING_FOR_APPROVAL so the
        # fabric confirm semantics stay consistent end to end.
        if receipt.get("needs_confirm"):
            return OpResult(
                status=OpStatus.WAITING_FOR_APPROVAL,
                service="files",
                operation=operation,
                availability=Availability.NATIVE,
                payload={"prepared": True, "sent": False, "receipt": receipt},
                error="confirmation_required",
            )
        return self._shape_local_result(operation, receipt)

    def _shape_local_result(self, operation: str, res: dict[str, Any]) -> OpResult:
        ok = bool(res.get("ok"))
        if ok:
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="files",
                operation=operation,
                availability=Availability.NATIVE,
                payload=res,
                verification={"source": "laptop_files", "verified": True},
            )
        error = str(res.get("error") or "failed")
        status = OpStatus.BLOCKED if error in {"path_denied", "denied"} else OpStatus.FAILED
        return OpResult(
            status=status,
            service="files",
            operation=operation,
            availability=Availability.NATIVE,
            error=error,
            payload=res,
        )


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
