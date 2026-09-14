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
        common = dict(
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
            wrapped = taint_external(observed, source="browser")
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="browser", operation=operation,
                            availability=Availability.OPERATED, payload=wrapped, taint=wrapped)
        if operation == "form_fill":
            before = await _computer("observe", args)
            acted = await _computer("act", {**args, "submit": False})
            after = await _computer("observe", args)
            return OpResult(
                status=OpStatus.PREPARED,
                service="browser",
                operation=operation,
                availability=Availability.OPERATED,
                payload={"before_ok": bool(before.get("ok", True)), "acted": acted, "after": after, "submitted": False},
            )
        if operation == "form_submit":
            before = await _computer("observe", args)
            if not before.get("ok", True) and before.get("error") == "computer_executor_unavailable":
                return OpResult(status=OpStatus.SERVICE_OFFLINE, service="browser", operation=operation,
                                availability=Availability.OPERATED, error="browser_offline",
                                diagnosis="browser_offline")
            acted = await _computer("act", {**args, "submit": True})
            after = await _computer("observe", args)
            expected = str(args.get("expected") or "")
            verified = (not expected) or expected.lower() in str(after).lower()
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED if verified else OpStatus.UNKNOWN,
                service="browser",
                operation=operation,
                availability=Availability.OPERATED,
                payload={"acted": acted, "after": after, "submitted": True},
                verification={"expected": expected, "verified": verified},
            )
        if operation == "download":
            result = await _computer("act", {**args, "download": True})
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="browser", operation=operation,
                            availability=Availability.OPERATED, payload={**result, "origin": "EXTERNAL_CONTENT"})
        return OpResult(status=OpStatus.FAILED, service="browser", operation=operation,
                        availability=Availability.OPERATED, error="unknown_operation")


def _is_owner_boundary(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in ("password", "otp", "captcha", "passkey", "2fa", "two-factor", "verify it's you")
    )


async def _computer(kind: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.ev.computer_executor import execute as computer_execute

        report = await computer_execute(kind, {**args, "activate": False})
        return report if isinstance(report, dict) else {"ok": False}
    except Exception as exc:
        return {"ok": False, "error": "computer_executor_unavailable", "diagnosis": type(exc).__name__}
