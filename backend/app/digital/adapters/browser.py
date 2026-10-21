"""Generic browser-backed service operations — reuse ComputerExecutor, no second agent."""

from __future__ import annotations

from typing import Any

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.taint import taint_external
from app.digital.types import Availability, OpStatus, Verb


class BrowserOperationsAdapter:
    slug = "browser"
    display_name = "Browser operations"

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        common: dict[str, Any] = dict(
            credential_type="browser_session",
            data_classification="external_web",
            backing="Home Station computer executor",
            supports_background=True,
        )
        return [
            cap("browser", "observe", Verb.READ, Availability.OPERATED, read_write="read", risk="R0",
                verification_method="reobserve", **common),
            cap("browser", "form_fill", Verb.PREPARE, Availability.OPERATED, read_write="write", risk="R1",
                verification_method="reobserve", **common),
            cap("browser", "form_submit", Verb.EXECUTE, Availability.OPERATED, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="reobserve_expected", **common),
            cap("browser", "download", Verb.DOWNLOAD, Availability.OPERATED, read_write="read", risk="R1",
                verification_method="artifact_hash", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        # Login / OTP / CAPTCHA / passkey are owner boundaries.
        page = str(args.get("page") or args.get("url") or args.get("goal") or "")
        if _is_owner_boundary(page) or _is_owner_boundary(str(args.get("text") or "")):
            return OpResult(
                status=OpStatus.BLOCKED,
                service="browser",
                operation=operation,
                availability=Availability.OPERATED,
                error="owner_authentication_boundary",
                diagnosis="captcha_or_password",
                payload={"prepared": True, "submitted": False},
            )
        if operation == "observe":
            observed = await _computer("observe", args)
            if _browser_down(observed):
                return _browser_offline(operation)
            if not _browser_ok(observed):
                return OpResult(
                    status=OpStatus.FAILED, service="browser", operation=operation,
                    availability=Availability.OPERATED,
                    error=str(observed.get("error") or "observe_failed"),
                    payload={"observed": False},
                )
            wrapped = taint_external(observed, source="browser")
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="browser", operation=operation,
                            availability=Availability.OPERATED, payload=wrapped, taint=wrapped)
        if operation == "form_fill":
            before = await _computer("observe", args)
            if _browser_down(before):
                return _browser_offline(operation)
            acted = await _computer("act", {**args, "submit": False})
            after = await _computer("observe", args)
            filled = _browser_ok(acted)
            return OpResult(
                status=OpStatus.PREPARED if filled else OpStatus.FAILED,
                service="browser",
                operation=operation,
                availability=Availability.OPERATED,
                payload={"before_ok": _browser_ok(before), "acted": acted, "after": after, "submitted": False},
                error=None if filled else str(acted.get("error") or "form_fill_failed"),
            )
        if operation == "form_submit":
            before = await _computer("observe", args)
            if _browser_down(before):
                return _browser_offline(operation)
            acted = await _computer("act", {**args, "submit": True})
            after = await _computer("observe", args)
            expected = str(args.get("expected") or "")
            submitted = _browser_ok(acted)
            matched = (not expected) or expected.lower() in str(after).lower()
            verified = submitted and _browser_ok(after) and matched
            if verified:
                status = OpStatus.COMPLETED_VERIFIED
            elif submitted and not matched:
                status = OpStatus.UNKNOWN
            else:
                status = OpStatus.FAILED
            return OpResult(
                status=status,
                service="browser",
                operation=operation,
                availability=Availability.OPERATED,
                payload={"acted": acted, "after": after, "submitted": submitted},
                verification={"expected": expected, "verified": verified},
                error=None if verified or status == OpStatus.UNKNOWN else str(
                    acted.get("error") or after.get("error") or "submit_not_confirmed"
                ),
            )
        if operation == "download":
            result = await _computer("act", {**args, "download": True})
            if _browser_down(result):
                return _browser_offline(operation)
            digest = result.get("sha256")
            artifact = result.get("artifact")
            if not digest and isinstance(artifact, dict):
                digest = artifact.get("sha256")
            verified = _browser_ok(result) and bool(digest)
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED if verified else OpStatus.FAILED,
                service="browser", operation=operation, availability=Availability.OPERATED,
                payload={**result, "origin": "EXTERNAL_CONTENT"},
                verification={"sha256": digest} if verified else None,
                error=None if verified else str(result.get("error") or "artifact_hash_missing"),
            )
        return OpResult(status=OpStatus.FAILED, service="browser", operation=operation,
                        availability=Availability.OPERATED, error="unknown_operation")


def _browser_down(report: dict[str, Any]) -> bool:
    return report.get("error") == "computer_executor_unavailable"


def _browser_ok(report: dict[str, Any]) -> bool:
    return report.get("ok") is not False and not report.get("error")


def _browser_offline(operation: str) -> OpResult:
    return OpResult(
        status=OpStatus.SERVICE_OFFLINE,
        service="browser",
        operation=operation,
        availability=Availability.OPERATED,
        error="browser_offline",
        diagnosis="browser_offline",
        payload={"submitted": False},
    )


def _is_owner_boundary(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in ("password", "otp", "captcha", "passkey", "2fa", "two-factor", "verify it's you")
    )


async def _computer(kind: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.ev import computer_executor as _executor

        computer_execute: Any = getattr(_executor, "execute", None)
        if computer_execute is None:
            return {"ok": False, "error": "computer_executor_unavailable", "diagnosis": "missing_execute_entrypoint"}
        report = await computer_execute(kind, {**args, "activate": False})
        return report if isinstance(report, dict) else {"ok": False}
    except Exception as exc:
        return {"ok": False, "error": "computer_executor_unavailable", "diagnosis": type(exc).__name__}
