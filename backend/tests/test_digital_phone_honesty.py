"""Phone adapter honesty — no fabricated success for unwired phone operations."""

from __future__ import annotations

import pytest

from app.digital.fabric import OpContext, OpResult, execute
from app.digital.types import Availability, OpStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ask", "approve", "capture_share"])
async def test_unwired_phone_operations_refuse_honestly(operation: str) -> None:
    result = await execute("phone", operation, {}, ctx=OpContext())
    assert result.status is OpStatus.FAILED
    assert result.error == "phone_channel_unavailable"
    assert "no device bridge wired for phone." in (result.diagnosis or "")
    assert result.operation == operation
    model = result.as_model()
    assert model["ok"] is False
    assert model["status"] == "FAILED"
    # Refused, not performed: the payload must never claim ok/completed.
    assert model["payload"]["refused"] is True
    assert model["payload"]["performed"] is False
    assert model["payload"].get("ok") is None


@pytest.mark.asyncio
async def test_phone_call_stays_prepared_not_connected() -> None:
    result = await execute("phone", "call", {}, ctx=OpContext())
    assert result.status is OpStatus.PREPARED
    assert result.as_model()["ok"] is True
    assert result.payload["connected"] is False


@pytest.mark.asyncio
async def test_phone_physical_action_stays_failed() -> None:
    result = await execute("phone", "physical_action", {}, ctx=OpContext())
    assert result.status is OpStatus.FAILED
    assert result.availability is Availability.UNAVAILABLE
    assert result.as_model()["ok"] is False


@pytest.mark.parametrize(
    ("status", "expected_ok"),
    [
        (OpStatus.COMPLETED_VERIFIED, True),
        (OpStatus.PREPARED, True),
        (OpStatus.WAITING_FOR_APPROVAL, False),
        (OpStatus.SERVICE_AUTH_REQUIRED, False),
        (OpStatus.BLOCKED, False),
        (OpStatus.FAILED, False),
        (OpStatus.UNKNOWN, False),
    ],
)
def test_as_model_ok_matches_status(status: OpStatus, expected_ok: bool) -> None:
    model = OpResult(
        status=status,
        service="gmail",
        operation="search",
        availability=Availability.NATIVE,
    ).as_model()
    assert model["ok"] is expected_ok
    assert model["status"] == status.value
